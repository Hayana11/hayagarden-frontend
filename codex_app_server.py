"""Small synchronous client for the local ``codex app-server`` process.

The web gateway owns one long-lived stdio child process.  Calls are serialized
because group chat already permits only one active generation, which keeps the
JSON-RPC lifecycle simple and avoids exposing the experimental websocket
transport on the network.
"""

from __future__ import annotations

from collections import deque
import json
import os
import queue
import signal
import shutil
import subprocess
import threading
import time
from typing import Iterator

import group_chat_store


DEFAULT_DB_PATH = os.environ.get("HAYA_DB_PATH", "/opt/frontend/memories.db")
DEFAULT_CWD = os.environ.get("CODEX_CHAT_CWD", "/tmp/hayagarden-codex-chat")
DEFAULT_CODEX_HOME = os.environ.get("CODEX_HOME", "/root/.codex")
NEXUS_PERMISSION_PROFILE = "hayagarden_nexus"
_CODEX_CANDIDATES = (
    os.environ.get("CODEX_BIN", ""),
    "/root/.local/bin/codex",
    "/usr/local/bin/codex",
    "/usr/bin/codex",
)


class CodexAppServerError(RuntimeError):
    pass


def find_codex() -> str | None:
    discovered = shutil.which("codex")
    for candidate in (*_CODEX_CANDIDATES, discovered or ""):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


_status_lock = threading.Lock()
_status_cached_at = 0.0
_status_cached: dict | None = None


def runtime_status(*, force: bool = False) -> dict:
    """Return install/auth readiness without exposing any credential."""
    global _status_cached_at, _status_cached
    with _status_lock:
        now = time.monotonic()
        if not force and _status_cached is not None and now - _status_cached_at < 8:
            return dict(_status_cached)
        binary = find_codex()
        result = {
            "installed": bool(binary),
            "authenticated": False,
            "ready": False,
            "detail": "蓝色线路尚未安装",
        }
        if binary:
            env = dict(os.environ)
            env.setdefault("HOME", "/root")
            env["CODEX_HOME"] = DEFAULT_CODEX_HOME
            try:
                probe = subprocess.run(
                    [binary, "login", "status"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=6,
                    env=env,
                )
                result["authenticated"] = probe.returncode == 0
                result["ready"] = probe.returncode == 0
                result["detail"] = (
                    "可以回复" if probe.returncode == 0 else "蓝色线路等待登录"
                )
            except (OSError, subprocess.SubprocessError):
                result["detail"] = "蓝色线路状态检查失败"
        _status_cached_at = now
        _status_cached = dict(result)
        return result


class CodexAppServer:
    def __init__(
        self,
        *,
        db_path: str = DEFAULT_DB_PATH,
        cwd: str = DEFAULT_CWD,
        sandbox: str = "read-only",
        env_mode: str = "inherit",
        codex_home: str | None = None,
        ephemeral_threads: bool = False,
        service_name: str | None = None,
    ):
        self.db_path = db_path
        self.cwd = cwd
        # Nexus may use workspace-write on a dedicated instance. Default remains
        # read-only so group-chat / monopoly callers keep their existing posture.
        if sandbox == "danger-full-access":
            raise ValueError("danger-full-access is not allowed")
        self.sandbox = sandbox if sandbox in {"read-only", "workspace-write"} else "read-only"
        # env_mode:
        #   inherit — existing group-chat / monopoly behavior (full os.environ)
        #   nexus_allowlist — minimal env for Nexus only (no app secrets)
        if env_mode not in {"inherit", "nexus_allowlist"}:
            raise ValueError("invalid env_mode")
        self.env_mode = env_mode
        self._nexus_mode = env_mode == "nexus_allowlist"
        self.codex_home = codex_home
        self.ephemeral_threads = bool(ephemeral_threads)
        self.service_name = service_name
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._process_pgid: int | None = None
        self._stop_confirmed = True
        self._last_start_result: dict | None = None
        self._session_resumed = False
        self._last_resume_error: str | None = None
        self._messages: queue.Queue = queue.Queue()
        self._request_id = 0
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._active_turn_id: str | None = None
        self._active_thread_id: str | None = None
        self._cancel_requested = threading.Event()
        self._interrupt_rpc_sent = threading.Event()
        self._interrupt_request_id: int | None = None
        self._last_interrupt_params: dict | None = None
        self._last_environment: dict | None = None

    def _environment(self) -> dict:
        if self.env_mode == "nexus_allowlist":
            env = self._nexus_allowlist_environment()
        else:
            env = dict(os.environ)
            env.setdefault("HOME", "/root")
            env["CODEX_HOME"] = self.codex_home or DEFAULT_CODEX_HOME
            local_bin = os.path.dirname(find_codex() or "")
            if local_bin:
                env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
        self._last_environment = dict(env)
        return env

    def _nexus_allowlist_environment(self) -> dict:
        """Minimal env for Nexus-owned Codex processes.

        Does not inherit BOARD_TOKEN / DB passwords / API keys / app secrets.
        """
        allow = (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "TMPDIR",
            "TMP",
            "TEMP",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
        env: dict[str, str] = {}
        for key in allow:
            value = os.environ.get(key)
            if value is not None and value != "":
                env[key] = value
        env.setdefault("PATH", "/usr/bin:/bin")
        env.setdefault("HOME", "/tmp")
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")
        # Independent Nexus CODEX_HOME — never default to /root/.codex for writes.
        home = self.codex_home
        if not home:
            raise CodexAppServerError("Nexus Codex requires an explicit codex_home")
        env["CODEX_HOME"] = home
        local_bin = os.path.dirname(find_codex() or "")
        if local_bin:
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
        return env

    def _reader(self, process: subprocess.Popen, output_queue: queue.Queue) -> None:
        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    output_queue.put(json.loads(line))
                except json.JSONDecodeError:
                    self._stderr_tail.append("invalid stdout: " + line[:300])
        finally:
            output_queue.put({"_transport_closed": True})

    def _stderr_reader(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stderr:
                line = line.strip()
                if line:
                    self._stderr_tail.append(line[:500])
        except Exception:
            pass

    def _nexus_auth_status(self) -> dict:
        """Probe login using the same allowlist env + CODEX_HOME as the Nexus server."""
        binary = find_codex()
        result = {
            "installed": bool(binary),
            "authenticated": False,
            "ready": False,
            "detail": "蓝色线路尚未安装",
        }
        if not binary:
            return result
        env = self._nexus_allowlist_environment()
        try:
            probe = subprocess.run(
                [binary, "login", "status"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=6,
                env=env,
            )
            result["authenticated"] = probe.returncode == 0
            result["ready"] = probe.returncode == 0
            result["detail"] = (
                "可以回复" if probe.returncode == 0 else "Nexus CODEX_HOME 等待登录"
            )
        except (OSError, subprocess.SubprocessError):
            result["detail"] = "Nexus CODEX_HOME 状态检查失败"
        return result

    def _ensure_nexus_permission_profile(self) -> None:
        """Merge the Nexus permission profile into an existing private CODEX_HOME."""
        home = self.codex_home
        if not home or not os.path.isdir(home):
            raise CodexAppServerError(
                "Nexus CODEX_HOME must already exist and be a directory"
            )

        config_path = os.path.join(home, "config.toml")
        existing = ""
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as handle:
                    existing = handle.read()
        except OSError as exc:
            raise CodexAppServerError(
                f"Nexus CODEX_HOME config.toml could not be read: {exc}"
            ) from exc

        # Preserve unrelated top-level keys and tables. Replace only the Nexus
        # profile tables and the top-level default_permissions assignment.
        kept: list[str] = []
        section = ""
        skip_nexus_section = False
        for line in existing.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1].strip()
                skip_nexus_section = (
                    section == f"permissions.{NEXUS_PERMISSION_PROFILE}"
                    or section.startswith(
                        f"permissions.{NEXUS_PERMISSION_PROFILE}."
                    )
                )
                if skip_nexus_section:
                    continue
            if skip_nexus_section:
                continue
            if section == "" and stripped.split("=", 1)[0].strip() == "default_permissions":
                continue
            kept.append(line)

        home_key = json.dumps(os.path.abspath(home), ensure_ascii=False)
        profile = [
            f'default_permissions = "{NEXUS_PERMISSION_PROFILE}"',
            "",
            f"[permissions.{NEXUS_PERMISSION_PROFILE}]",
            'description = "HayaGarden Nexus hard isolation"',
            "",
            f"[permissions.{NEXUS_PERMISSION_PROFILE}.filesystem]",
            '":minimal" = "read"',
            f"{home_key} = \"deny\"",
            '"/opt/frontend" = "deny"',
            '"/root/.codex" = "deny"',
            "",
            f'[permissions.{NEXUS_PERMISSION_PROFILE}.filesystem.":workspace_roots"]',
            '"." = "write"',
            "",
            f"[permissions.{NEXUS_PERMISSION_PROFILE}.network]",
            "enabled = false",
        ]
        prefix = "\n".join(kept).rstrip()
        merged = (prefix + "\n\n" if prefix else "") + "\n".join(profile) + "\n"
        temporary = config_path + ".nexus.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(merged)
            os.replace(temporary, config_path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise CodexAppServerError(
                f"Nexus CODEX_HOME config.toml could not be updated: {exc}"
            ) from exc

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self._nexus_mode:
            if not self.codex_home or not os.path.isdir(self.codex_home):
                raise CodexAppServerError(
                    "Nexus CODEX_HOME must already exist and be a directory"
                )
            self._ensure_nexus_permission_profile()
        binary = find_codex()
        if not binary:
            raise CodexAppServerError("蓝色线路尚未安装")
        if self._nexus_mode:
            status = self._nexus_auth_status()
        else:
            status = runtime_status(force=True)
        if not status["authenticated"]:
            raise CodexAppServerError(status.get("detail") or "蓝色线路等待登录")
        os.makedirs(self.cwd, exist_ok=True)
        if self.codex_home and not self._nexus_mode:
            os.makedirs(self.codex_home, exist_ok=True)
        self._messages = queue.Queue()
        self._stderr_tail.clear()
        popen_kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "bufsize": 1,
            "cwd": self.cwd,
            "env": self._environment(),
        }
        if self._nexus_mode:
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(
                [binary, "app-server", "--listen", "stdio://"],
                **popen_kwargs,
            )
        except OSError as exc:
            raise CodexAppServerError(f"蓝色线路启动失败：{exc}") from exc
        self._process = process
        self._stop_confirmed = False
        if self._nexus_mode:
            try:
                self._process_pgid = os.getpgid(process.pid)
            except OSError as exc:
                try:
                    process.terminate()
                except Exception:
                    pass
                self._process = None
                raise CodexAppServerError(
                    f"Nexus Codex process group could not be recorded: {exc}"
                ) from exc
        threading.Thread(
            target=self._reader, args=(process, self._messages), daemon=True
        ).start()
        threading.Thread(target=self._stderr_reader, args=(process,), daemon=True).start()
        client_name = "hayagarden-nexus" if self.env_mode == "nexus_allowlist" else "hayagarden-group-chat"
        self._request_locked(
            "initialize",
            {
                "clientInfo": {
                    "name": client_name,
                    "title": "HayaGarden Nexus" if self.env_mode == "nexus_allowlist" else "HayaGarden Group Chat",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": False},
            },
            timeout=20,
            ensure_started=False,
        )
        self._send_locked({"method": "initialized", "params": {}})

    def _stop_locked(self) -> bool:
        if self._nexus_mode:
            return self._stop_process_tree_locked()
        process, self._process = self._process, None
        if process is None:
            return True
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=4)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        return process.poll() is not None

    def clean_background_terminals(self, thread_id: str | None) -> bool:
        """Best-effort cleanup of terminals owned by one Nexus thread."""
        if not thread_id:
            return True
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return True
            try:
                self._request_locked(
                    "thread/backgroundTerminals/clean",
                    {"threadId": thread_id},
                    timeout=2,
                    ensure_started=False,
                )
                return True
            except Exception:
                return False

    @staticmethod
    def _process_group_alive(pgid: int | None) -> bool:
        if not pgid or pgid == os.getpgrp():
            return False
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True

    def _stop_process_tree_locked(
        self, *, soft_timeout: float = 4, hard_timeout: float = 3
    ) -> bool:
        process = self._process
        pgid = self._process_pgid
        thread_id = self._active_thread_id
        self._stop_confirmed = process is None or process.poll() is not None

        if process is not None:
            self.clean_background_terminals(thread_id)
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=max(0.0, soft_timeout))
            except Exception:
                pass

            if self._process_group_alive(pgid):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

            deadline = time.monotonic() + max(0.0, hard_timeout)
            while self._process_group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(0.05)

            if self._process_group_alive(pgid):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=1)
            except Exception:
                pass
            self._stop_confirmed = process.poll() is not None

        self._process = None
        self._process_pgid = None
        self._active_turn_id = None
        self._active_thread_id = None
        return self._stop_confirmed

    def stop_process_tree(
        self, *, soft_timeout: float = 4, hard_timeout: float = 3
    ) -> bool:
        """Stop the Nexus app-server and every process in its session group."""
        with self._lock:
            if not self._nexus_mode:
                return self._stop_locked()
            return self._stop_process_tree_locked(
                soft_timeout=soft_timeout, hard_timeout=hard_timeout
            )

    def close(self) -> bool:
        with self._lock:
            if self._nexus_mode:
                return self._stop_process_tree_locked()
            return self._stop_locked()

    def stop(self) -> bool:
        """Public alias used by Nexus when interrupt is not provider-confirmed."""
        return self.close()

    def _send_locked(self, payload: dict) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise CodexAppServerError("蓝色线路连接已断开")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerError("蓝色线路连接已断开") from exc

    def _next_message_locked(self, timeout: float) -> dict:
        if timeout <= 0:
            raise CodexAppServerError("蓝色线路响应超时")
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexAppServerError("蓝色线路响应超时") from exc
        if message.get("_transport_closed"):
            detail = self._stderr_tail[-1] if self._stderr_tail else "进程已退出"
            raise CodexAppServerError("蓝色线路连接关闭：" + detail)
        return message

    def _answer_server_request_locked(self, message: dict) -> bool:
        if "id" not in message or "method" not in message:
            return False
        method = str(message.get("method") or "")
        if method == "item/permissions/requestApproval":
            self._send_locked({
                "id": message["id"],
                "result": {"permissions": {}, "scope": "turn"},
            })
            return True
        if method.endswith("requestApproval"):
            result = {"decision": "decline"}
        elif "elicitation" in method.lower():
            result = {"action": "decline", "content": None}
        else:
            self._send_locked({
                "id": message["id"],
                "error": {"code": -32601, "message": "Unsupported client request"},
            })
            return True
        self._send_locked({"id": message["id"], "result": result})
        return True

    def _request_locked(
        self,
        method: str,
        params: dict,
        *,
        timeout: float = 30,
        ensure_started: bool = True,
        early_notifications: list[dict] | None = None,
    ) -> dict:
        if ensure_started:
            self._start_locked()
        self._request_id += 1
        request_id = self._request_id
        self._send_locked({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self._next_message_locked(deadline - time.monotonic())
            if self._answer_server_request_locked(message):
                continue
            if message.get("id") == request_id:
                if message.get("error"):
                    error = message["error"]
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise CodexAppServerError(f"{method} 失败：{detail}")
                return message.get("result") or {}
            if message.get("method") and early_notifications is not None:
                early_notifications.append(message)

    @staticmethod
    def _agent_text_from_item(item: dict | None) -> str:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return ""
        return str(item.get("text") or "")

    def _ensure_thread_locked(self, room: str, instructions: str) -> str:
        binding = group_chat_store.get_thread_binding(
            room, "codex", db_path=self.db_path
        )
        common = {
            "cwd": self.cwd,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "developerInstructions": instructions,
            "sandbox": self.sandbox,
            "personality": "friendly",
        }
        if binding:
            try:
                self._request_locked(
                    "thread/resume",
                    {"threadId": binding["thread_id"], **common},
                    timeout=30,
                )
                return binding["thread_id"]
            except CodexAppServerError:
                group_chat_store.delete_thread_binding(
                    room, "codex", db_path=self.db_path
                )
        result = self._request_locked(
            "thread/start",
            {**common, "serviceName": "hayagarden_group_chat"},
            timeout=30,
        )
        thread = result.get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise CodexAppServerError("蓝色线路没有返回会话编号")
        group_chat_store.save_thread_binding(
            room, "codex", thread_id, db_path=self.db_path
        )
        return thread_id

    def _ensure_bound_thread_locked(self, thread_id: str | None, instructions: str) -> str:
        """Resume/start a caller-owned thread without using group-chat storage.

        Monopoly keeps its binding in its own room record, so game history and
        the existing group-chat store remain strictly separate.

        Nexus ephemeral threads are in-process only (path must stay null) but the
        same app-server process may resume the prior ephemeral thread id.
        """
        if self._nexus_mode:
            common = {
                "cwd": self.cwd,
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "developerInstructions": instructions,
                "permissions": NEXUS_PERMISSION_PROFILE,
                "runtimeWorkspaceRoots": [self.cwd],
                "personality": "friendly",
            }
            resume_common = dict(common)
            start_common = {**common, "ephemeral": True}
        else:
            common = {
                "cwd": self.cwd,
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "developerInstructions": instructions,
                "sandbox": self.sandbox,
                "personality": "friendly",
            }
            if self.ephemeral_threads:
                common["ephemeral"] = True
            resume_common = common
            start_common = common

        self._session_resumed = False
        self._last_resume_error = None
        if thread_id:
            try:
                result = self._request_locked(
                    "thread/resume",
                    {"threadId": thread_id, **resume_common},
                    timeout=30,
                )
            except CodexAppServerError as exc:
                self._last_resume_error = str(exc)
            else:
                if self._nexus_mode:
                    self._record_nexus_thread_result(result, operation="resume")
                self._session_resumed = True
                return thread_id
        service = self.service_name or (
            "hayagarden_nexus" if self.ephemeral_threads else "hayagarden_monopoly"
        )
        start_params = {**start_common, "serviceName": service}
        result = self._request_locked(
            "thread/start",
            start_params,
            timeout=30,
        )
        if self._nexus_mode:
            self._record_nexus_thread_result(result, operation="start")
        new_id = str((result.get("thread") or {}).get("id") or "")
        if not new_id:
            raise CodexAppServerError("Codex game thread did not return an id")
        thread = result.get("thread") or {}
        if self.ephemeral_threads and thread.get("path"):
            raise CodexAppServerError("ephemeral Codex thread unexpectedly returned a path")
        return new_id

    def _record_nexus_thread_result(self, result: dict, *, operation: str) -> None:
        """Fail closed unless Codex confirms the requested native profile."""
        thread = result.get("thread") if isinstance(result, dict) else None
        thread = thread if isinstance(thread, dict) else {}
        profile = result.get("activePermissionProfile") if isinstance(result, dict) else None
        if profile is None:
            profile = thread.get("activePermissionProfile")
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        if profile_id != NEXUS_PERMISSION_PROFILE:
            state = "missing" if not profile_id else f"unexpected {profile_id!r}"
            raise CodexAppServerError(
                f"thread/{operation} did not activate {NEXUS_PERMISSION_PROFILE}: {state}"
            )

        roots = result.get("runtimeWorkspaceRoots") if isinstance(result, dict) else None
        if roots is None:
            roots = thread.get("runtimeWorkspaceRoots")
        recorded = dict(result)
        recorded["runtimeWorkspaceRoots"] = roots
        self._last_start_result = recorded

    def interrupt_turn(self, turn_id: str, thread_id: str | None = None) -> None:
        """Request turn/interrupt with both threadId and turnId.

        Sets the cancel flag immediately and never blocks waiting for the stream
        lock. Sends at most one interrupt RPC per turn (shared with the stream loop).
        """
        self._cancel_requested.set()
        tid = thread_id or self._active_thread_id
        params = {"turnId": turn_id}
        if tid:
            params["threadId"] = tid
        self._last_interrupt_params = dict(params)
        if self._interrupt_rpc_sent.is_set():
            return
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            if self._interrupt_rpc_sent.is_set():
                return
            if not self._process or self._process.poll() is not None:
                return
            if not turn_id:
                return
            try:
                self._request_id += 1
                request_id = self._request_id
                self._interrupt_request_id = request_id
                self._send_locked(
                    {
                        "id": request_id,
                        "method": "turn/interrupt",
                        "params": params,
                    }
                )
                self._interrupt_rpc_sent.set()
            except Exception:
                return
        finally:
            self._lock.release()

    def interrupt_active_turn(self) -> None:
        turn_id = self._active_turn_id
        thread_id = self._active_thread_id
        self._cancel_requested.set()
        if turn_id:
            self.interrupt_turn(turn_id, thread_id)

    def _send_interrupt_rpc(self, thread_id: str, turn_id: str) -> bool:
        """Send turn/interrupt under a short lock. Returns True if sent or already sent."""
        params = {"threadId": thread_id, "turnId": turn_id}
        self._last_interrupt_params = dict(params)
        if self._interrupt_rpc_sent.is_set():
            return True
        with self._lock:
            if self._interrupt_rpc_sent.is_set():
                return True
            if not self._process or self._process.poll() is not None:
                return False
            try:
                self._request_id += 1
                request_id = self._request_id
                self._interrupt_request_id = request_id
                self._send_locked(
                    {
                        "id": request_id,
                        "method": "turn/interrupt",
                        "params": params,
                    }
                )
                self._interrupt_rpc_sent.set()
                return True
            except Exception:
                return False

    def _next_message(self, timeout: float) -> dict:
        """Read the next transport message without requiring the server lock."""
        if timeout <= 0:
            raise CodexAppServerError("蓝色线路响应超时")
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexAppServerError("蓝色线路响应超时") from exc
        if message.get("_transport_closed"):
            detail = self._stderr_tail[-1] if self._stderr_tail else "进程已退出"
            raise CodexAppServerError("蓝色线路连接关闭：" + detail)
        return message

    def stream_bound_turn(
        self,
        thread_id: str | None,
        instructions: str,
        prompt: str,
        *,
        timeout: float = 360,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[tuple[str, object]]:
        """Stream a turn whose thread binding is persisted by the caller.

        The server lock is held only for short setup / send / answer sections.
        Blocking waits and yields happen outside the lock so interrupt can run.

        Interrupt completion requires provider ``turn/completed`` with
        ``status=interrupted`` — a local cancel flag alone is insufficient.
        """
        # Preserve a cancel that was already requested before this turn started
        # (immediate POST→interrupt race). Only clear when no cancel is pending.
        pending_cancel = self._cancel_requested.is_set() or (
            cancel_event is not None and cancel_event.is_set()
        )
        if not pending_cancel:
            self._cancel_requested.clear()
            self._interrupt_rpc_sent.clear()
            self._interrupt_request_id = None
            self._last_interrupt_params = None

        turn_id: str | None = None
        bound_id: str | None = None
        with self._lock:
            self._start_locked()
            bound_id = self._ensure_bound_thread_locked(thread_id, instructions)
            self._active_thread_id = bound_id
            early: list[dict] = []
            result = self._request_locked(
                "turn/start",
                {
                    "threadId": bound_id,
                    "input": [{"type": "text", "text": prompt}],
                    "approvalPolicy": "never",
                },
                timeout=30,
                early_notifications=early,
            )
            turn_id = str((result.get("turn") or {}).get("id") or "")
            if not turn_id:
                raise CodexAppServerError("Codex game turn did not return an id")
            self._active_turn_id = turn_id
            buffered = deque(early)

        # Publish IDs immediately after turn/start so adapters can sync before done.
        yield "meta", {
            "phase": "turn_started",
            "thread_id": bound_id,
            "turn_id": turn_id,
        }

        deadline = time.monotonic() + timeout
        saw_delta = False
        waiting_interrupt_ack = False
        try:
            while True:
                cancel_now = self._cancel_requested.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                )
                if cancel_now and turn_id and bound_id:
                    self._cancel_requested.set()
                    if not self._interrupt_rpc_sent.is_set():
                        sent = self._send_interrupt_rpc(bound_id, turn_id)
                        if not sent:
                            yield "err", {
                                "code": "interrupt_failed",
                                "message": "failed to send turn/interrupt",
                            }
                            return
                    waiting_interrupt_ack = True

                try:
                    if buffered:
                        message = buffered.popleft()
                    else:
                        message = self._next_message(deadline - time.monotonic())
                except CodexAppServerError:
                    with self._lock:
                        self._stop_locked()
                    if waiting_interrupt_ack:
                        yield "err", {
                            "code": "interrupt_unconfirmed",
                            "message": "provider did not confirm interrupted status",
                        }
                        return
                    raise

                with self._lock:
                    answered = self._answer_server_request_locked(message)
                if answered:
                    continue

                # Match interrupt JSON-RPC response by request id only.
                msg_id = message.get("id")
                if (
                    waiting_interrupt_ack
                    and msg_id is not None
                    and self._interrupt_request_id is not None
                    and msg_id == self._interrupt_request_id
                ):
                    if message.get("error") is not None:
                        detail = message["error"]
                        msg = detail.get("message") if isinstance(detail, dict) else str(detail)
                        yield "err", {
                            "code": "interrupt_rejected",
                            "message": msg or "provider rejected turn/interrupt",
                        }
                        return
                    # Successful interrupt RPC ack — still wait for turn/completed.
                    continue

                method = message.get("method")
                params = message.get("params") or {}
                if params.get("threadId") not in (None, bound_id):
                    continue
                if params.get("turnId") not in (None, turn_id):
                    continue
                if method == "item/agentMessage/delta":
                    if waiting_interrupt_ack:
                        continue
                    delta = str(params.get("delta") or "")
                    if delta:
                        saw_delta = True
                        yield "text", delta
                elif method == "item/completed" and not saw_delta and not waiting_interrupt_ack:
                    text = self._agent_text_from_item(params.get("item"))
                    if text:
                        saw_delta = True
                        yield "text", text
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    status = turn.get("status")
                    cancel_now = self._cancel_requested.is_set() or (
                        cancel_event is not None and cancel_event.is_set()
                    )
                    if waiting_interrupt_ack or cancel_now:
                        if status == "interrupted":
                            yield "err", {
                                "code": "interrupted",
                                "message": "turn interrupted",
                                "provider_status": status,
                            }
                            return
                        yield "err", {
                            "code": "interrupt_unconfirmed",
                            "message": f"expected interrupted, got {status!r}",
                            "provider_status": status,
                        }
                        return
                    if status != "completed":
                        error = turn.get("error") or {}
                        raise CodexAppServerError(
                            error.get("message") or f"Codex turn status: {status}"
                        )
                    if not saw_delta:
                        for item in turn.get("items") or []:
                            text = self._agent_text_from_item(item)
                            if text:
                                saw_delta = True
                                yield "text", text
                    yield "done", {
                        "thread_id": bound_id,
                        "turn_id": turn_id,
                        "status": status,
                    }
                    return
                elif method == "error":
                    error = params.get("error") or params
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise CodexAppServerError("Codex game line error: " + detail)
        except Exception:
            with self._lock:
                self._stop_locked()
            raise
        finally:
            with self._lock:
                if turn_id is not None and self._active_turn_id == turn_id:
                    self._active_turn_id = None
                # Keep _active_thread_id for in-process ephemeral continuity until
                # a new turn binds a different id; clear only the finished turn.

    def stream_turn(
        self,
        room: str,
        instructions: str,
        prompt: str,
        *,
        timeout: float = 360,
    ) -> Iterator[tuple[str, object]]:
        """Yield ``('text', delta)`` and one final ``('done', metadata)``."""
        with self._lock:
            try:
                self._start_locked()
                thread_id = self._ensure_thread_locked(room, instructions)
                early: list[dict] = []
                result = self._request_locked(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                        "approvalPolicy": "never",
                    },
                    timeout=30,
                    early_notifications=early,
                )
                turn_id = str((result.get("turn") or {}).get("id") or "")
                if not turn_id:
                    raise CodexAppServerError("蓝色线路没有返回本轮编号")
                deadline = time.monotonic() + timeout
                saw_delta = False
                buffered = deque(early)
                while True:
                    message = (
                        buffered.popleft()
                        if buffered
                        else self._next_message_locked(deadline - time.monotonic())
                    )
                    if self._answer_server_request_locked(message):
                        continue
                    method = message.get("method")
                    params = message.get("params") or {}
                    if params.get("threadId") not in (None, thread_id):
                        continue
                    if params.get("turnId") not in (None, turn_id):
                        continue
                    if method == "item/agentMessage/delta":
                        delta = str(params.get("delta") or "")
                        if delta:
                            saw_delta = True
                            yield "text", delta
                    elif method == "item/completed" and not saw_delta:
                        text = self._agent_text_from_item(params.get("item"))
                        if text:
                            saw_delta = True
                            yield "text", text
                    elif method == "turn/completed":
                        turn = params.get("turn") or {}
                        status = turn.get("status")
                        if status != "completed":
                            error = turn.get("error") or {}
                            detail = error.get("message") or f"本轮状态：{status}"
                            raise CodexAppServerError("蓝色线路回复失败：" + detail)
                        if not saw_delta:
                            for item in turn.get("items") or []:
                                text = self._agent_text_from_item(item)
                                if text:
                                    saw_delta = True
                                    yield "text", text
                        yield "done", {
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "status": status,
                        }
                        return
                    elif method == "error":
                        error = params.get("error") or params
                        detail = error.get("message") if isinstance(error, dict) else str(error)
                        raise CodexAppServerError("蓝色线路错误：" + detail)
            except Exception:
                # A timed-out or failed turn can leave unread events in the
                # transport. Restart cleanly and resume the persisted thread on
                # the next request instead of reusing an ambiguous stream.
                self._stop_locked()
                raise


client = CodexAppServer()
