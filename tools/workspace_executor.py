"""
workspace_executor.py — sandboxed shell execution for /opt/workspace.

Commands never run as the gateway user. Default EXEC_ENABLED=0; when disabled
the tool returns exec_disabled without spawning a subprocess.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid

EXEC_ENABLED = os.environ.get("EXEC_ENABLED", "0") == "1"
EXEC_USER = os.environ.get("EXEC_USER", "wsandbox")
EXEC_CWD = os.environ.get("EXEC_CWD", "/opt/workspace")
EXEC_TIMEOUT = int(os.environ.get("EXEC_TIMEOUT", "300"))

EXEC_ENV = {
    "HOME": f"/home/{EXEC_USER}",
    "USER": EXEC_USER,
    "LOGNAME": EXEC_USER,
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "SHELL": "/bin/bash",
    "TERM": "xterm",
}

for _name in os.environ.get("EXEC_PASS_ENV", "").split(","):
    _name = _name.strip()
    if _name and _name in os.environ:
        EXEC_ENV[_name] = os.environ[_name]

_MAX_OUTPUT_CHARS = 12000
_INLINE_MAX_CHARS = 8000
_HEAD_LINES = 80
_TAIL_LINES = 40
_STDERR_TAIL_LINES = 120
_PREVIEW_STDOUT_CAP = 6000
_PREVIEW_STDERR_CAP = 2500
_OUTPUTS_DIR = os.path.join(EXEC_CWD, "artifacts", "tool_outputs")

_BLOCKED: list[re.Pattern] = [
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r":\s*\(\)\s*\{", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\breboot\b", re.IGNORECASE),
    re.compile(r"\bpasswd\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+-?[a-zA-Z]*\s*777", re.IGNORECASE),
    re.compile(r">\s*/dev/sd", re.IGNORECASE),
]


def _head_tail_preview(text: str, head: int, tail: int, cap: int) -> str:
    lines = text.splitlines()
    if len(lines) <= head + tail:
        preview = text
    else:
        skipped = len(lines) - head - tail
        parts: list[str] = []
        if head:
            parts.append("\n".join(lines[:head]))
        parts.append(f"... [{skipped} lines omitted, full output saved to disk] ...")
        if tail:
            parts.append("\n".join(lines[-tail:]))
        preview = "\n".join(parts)
    if len(preview) > cap:
        preview = preview[:cap] + "\n... [preview capped]"
    return preview


def _spill_to_disk(cmd: str, stdout: str, stderr: str) -> str:
    os.makedirs(_OUTPUTS_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = os.path.join(_OUTPUTS_DIR, f"{stamp}-{uuid.uuid4().hex[:6]}.log")
    with open(path, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"$ {cmd}\n\n===== STDOUT =====\n{stdout}\n\n===== STDERR =====\n{stderr}\n")
    os.chmod(path, 0o660)
    return path


def _build_result(cmd: str, exit_code: int | None, stdout: str, stderr: str,
                  extra: dict | None = None) -> str:
    if len(stdout) + len(stderr) <= _INLINE_MAX_CHARS:
        body = {"exit_code": exit_code, "stdout": stdout, "stderr": stderr, "truncated": False}
    else:
        try:
            full_path = _spill_to_disk(cmd, stdout, stderr)
        except Exception as exc:
            body = {
                "exit_code": exit_code,
                "stdout": stdout[:_MAX_OUTPUT_CHARS],
                "stderr": stderr[:_MAX_OUTPUT_CHARS],
                "truncated": True,
                "spill_error": str(exc)[:200],
            }
        else:
            body = {
                "exit_code": exit_code,
                "stdout_preview": _head_tail_preview(
                    stdout, _HEAD_LINES, _TAIL_LINES, _PREVIEW_STDOUT_CAP
                ),
                "stderr_preview": _head_tail_preview(
                    stderr, 0, _STDERR_TAIL_LINES, _PREVIEW_STDERR_CAP
                ),
                "stdout_lines": len(stdout.splitlines()),
                "stderr_lines": len(stderr.splitlines()),
                "full_output_path": full_path,
                "truncated": True,
                "note": "long output spilled to disk; read full_output_path with ws_read if needed",
            }
    if extra:
        body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def _blocked_reason(cmd: str) -> str | None:
    for pat in _BLOCKED:
        if pat.search(cmd):
            return pat.pattern
    return None


def run_exec(cmd: str, secrets: object | None = None) -> str:
    """Run one shell command in the sandbox, return JSON for the model."""
    del secrets  # PR 1: no [KEY_n] round-trip into subprocess env
    cmd = (cmd or "").strip()
    if not cmd:
        return json.dumps({"error": "empty command"}, ensure_ascii=False)

    if not EXEC_ENABLED:
        return json.dumps(
            {
                "error": "exec_disabled",
                "detail": "shell execution is not enabled (set EXEC_ENABLED=1)",
            },
            ensure_ascii=False,
        )

    blocked = _blocked_reason(cmd)
    if blocked:
        return json.dumps(
            {"error": "blocked", "detail": f"command matched the slip blocklist: {blocked}"},
            ensure_ascii=False,
        )

    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", cmd],
            cwd=EXEC_CWD,
            user=EXEC_USER,
            group=EXEC_USER,
            extra_groups=[],
            env=EXEC_ENV,
            text=True,
            errors="replace",
            capture_output=True,
            timeout=EXEC_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        return _build_result(
            cmd,
            None,
            out,
            err,
            extra={
                "error": "timeout",
                "detail": f"command exceeded {EXEC_TIMEOUT}s and was terminated",
            },
        )
    except Exception as exc:
        return json.dumps(
            {"error": "exec_failed", "detail": str(exc)[:200]},
            ensure_ascii=False,
        )

    return _build_result(cmd, proc.returncode, proc.stdout or "", proc.stderr or "")
