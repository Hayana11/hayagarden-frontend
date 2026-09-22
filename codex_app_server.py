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
import shutil
import subprocess
import threading
import time
from typing import Iterator

import config_store
import group_chat_store


CODEX_CHAT_MODEL_KEY = "CODEX_CHAT_MODEL"
DEFAULT_DB_PATH = os.environ.get("HAYA_DB_PATH", "/opt/frontend/memories.db")
DEFAULT_CWD = os.environ.get("CODEX_CHAT_CWD", "/tmp/hayagarden-codex-chat")
DEFAULT_CODEX_HOME = os.environ.get("CODEX_HOME", "/root/.codex")
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
    def __init__(self, *, db_path: str = DEFAULT_DB_PATH, cwd: str = DEFAULT_CWD):
        self.db_path = db_path
        self.cwd = cwd
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._messages: queue.Queue = queue.Queue()
        self._request_id = 0
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._model_cache_at = 0.0
        self._model_cache: list[dict] = []

    def _environment(self) -> dict:
        env = dict(os.environ)
        env.setdefault("HOME", "/root")
        env["CODEX_HOME"] = DEFAULT_CODEX_HOME
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

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        binary = find_codex()
        if not binary:
            raise CodexAppServerError("蓝色线路尚未安装")
        status = runtime_status(force=True)
        if not status["authenticated"]:
            raise CodexAppServerError("蓝色线路等待登录")
        os.makedirs(self.cwd, exist_ok=True)
        self._messages = queue.Queue()
        self._stderr_tail.clear()
        try:
            process = subprocess.Popen(
                [binary, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=self._environment(),
            )
        except OSError as exc:
            raise CodexAppServerError(f"蓝色线路启动失败：{exc}") from exc
        self._process = process
        threading.Thread(
            target=self._reader, args=(process, self._messages), daemon=True
        ).start()
        threading.Thread(target=self._stderr_reader, args=(process,), daemon=True).start()
        self._request_locked(
            "initialize",
            {
                "clientInfo": {
                    "name": "hayagarden-group-chat",
                    "title": "HayaGarden Group Chat",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": False},
            },
            timeout=20,
            ensure_started=False,
        )
        self._send_locked({"method": "initialized", "params": {}})

    def _stop_locked(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=4)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._stop_locked()

    def configured_model(self) -> str:
        return str(config_store.get(CODEX_CHAT_MODEL_KEY, '') or '').strip()

    def set_configured_model(self, model: str | None) -> str:
        value = str(model or '').strip()
        config_store.set(CODEX_CHAT_MODEL_KEY, value)
        return value

    def list_models(self, *, force: bool = False) -> list[dict]:
        """Return the visible model catalog exposed by the logged-in app-server."""
        with self._lock:
            now = time.monotonic()
            if not force and self._model_cache and now - self._model_cache_at < 30:
                return [dict(row) for row in self._model_cache]
            self._start_locked()
            rows: list[dict] = []
            cursor: str | None = None
            while True:
                params = {"limit": 100, "includeHidden": False}
                if cursor:
                    params["cursor"] = cursor
                result = self._request_locked("model/list", params, timeout=20)
                for raw in result.get("data") or []:
                    if not isinstance(raw, dict):
                        continue
                    model_id = str(raw.get("id") or raw.get("model") or '').strip()
                    if not model_id:
                        continue
                    efforts = []
                    for effort in raw.get("supportedReasoningEfforts") or []:
                        if isinstance(effort, dict):
                            name = str(effort.get("reasoningEffort") or '').strip()
                            if name:
                                efforts.append(name)
                    rows.append({
                        "id": model_id,
                        "label": str(raw.get("displayName") or model_id),
                        "is_default": bool(raw.get("isDefault")),
                        "default_effort": str(raw.get("defaultReasoningEffort") or ''),
                        "efforts": efforts,
                        "input_modalities": list(raw.get("inputModalities") or ["text", "image"]),
                    })
                cursor = str(result.get("nextCursor") or '').strip() or None
                if not cursor:
                    break
            self._model_cache = [dict(row) for row in rows]
            self._model_cache_at = now
            return [dict(row) for row in rows]

    def resolved_model(self) -> tuple[str, str]:
        """Return (model_id, mode), resolving empty config to app-server default."""
        configured = self.configured_model()
        if configured:
            return configured, "explicit"
        try:
            models = self.list_models()
        except Exception:
            return '', "default"
        default = next((row.get("id") for row in models if row.get("is_default")), '')
        return str(default or ''), "default"

    def _turn_params(self, thread_id: str, prompt: str) -> tuple[dict, str, str]:
        model, mode = self.resolved_model()
        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "approvalPolicy": "never",
        }
        if model:
            params["model"] = model
        return params, model, mode

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
            "sandbox": "read-only",
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
        """
        common = {
            "cwd": self.cwd,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "developerInstructions": instructions,
            "sandbox": "read-only",
            "personality": "friendly",
        }
        if thread_id:
            try:
                self._request_locked(
                    "thread/resume", {"threadId": thread_id, **common}, timeout=30,
                )
                return thread_id
            except CodexAppServerError:
                pass
        result = self._request_locked(
            "thread/start",
            {**common, "serviceName": "hayagarden_monopoly"},
            timeout=30,
        )
        new_id = str((result.get("thread") or {}).get("id") or "")
        if not new_id:
            raise CodexAppServerError("Codex game thread did not return an id")
        return new_id

    def stream_bound_turn(
        self,
        thread_id: str | None,
        instructions: str,
        prompt: str,
        *,
        timeout: float = 360,
    ) -> Iterator[tuple[str, object]]:
        """Stream a turn whose thread binding is persisted by the caller."""
        with self._lock:
            try:
                self._start_locked()
                bound_id = self._ensure_bound_thread_locked(thread_id, instructions)
                early: list[dict] = []
                turn_params, turn_model, turn_model_mode = self._turn_params(bound_id, prompt)
                result = self._request_locked(
                    "turn/start",
                    turn_params,
                    timeout=30,
                    early_notifications=early,
                )
                turn_id = str((result.get("turn") or {}).get("id") or "")
                if not turn_id:
                    raise CodexAppServerError("Codex game turn did not return an id")
                deadline = time.monotonic() + timeout
                saw_delta = False
                buffered = deque(early)
                while True:
                    message = buffered.popleft() if buffered else self._next_message_locked(deadline - time.monotonic())
                    if self._answer_server_request_locked(message):
                        continue
                    method = message.get("method")
                    params = message.get("params") or {}
                    if params.get("threadId") not in (None, bound_id):
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
                            raise CodexAppServerError(error.get("message") or f"Codex turn status: {status}")
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
                            "model": turn_model,
                            "model_mode": turn_model_mode,
                        }
                        return
                    elif method == "error":
                        error = params.get("error") or params
                        detail = error.get("message") if isinstance(error, dict) else str(error)
                        raise CodexAppServerError("Codex game line error: " + detail)
            except Exception:
                self._stop_locked()
                raise

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
                turn_params, turn_model, turn_model_mode = self._turn_params(thread_id, prompt)
                result = self._request_locked(
                    "turn/start",
                    turn_params,
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
                            "model": turn_model,
                            "model_mode": turn_model_mode,
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
