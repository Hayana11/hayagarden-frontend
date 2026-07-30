"""Unified subprocess runner for Claude forge spike harness."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class SubprocessRunResult:
    exit_code: int
    stdout_lines: list[str] = field(default_factory=list)
    stderr_text: str = ''
    timed_out: bool = False
    process_started: bool = False


def run_subprocess_with_timeout(
    *,
    cmd: list[str],
    cwd: str,
    env: dict[str, str],
    stdin_payload: str,
    timeout_seconds: float,
    popen_factory: Optional[Callable[..., Any]] = None,
) -> SubprocessRunResult:
    """Run a subprocess with line-buffered stdout collection and process-group kill on timeout."""
    factory = popen_factory or subprocess.Popen
    proc = factory(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    result = SubprocessRunResult(exit_code=-1, process_started=True)
    stdout_lines: list[str] = []
    stderr_chunks: list[str] = []

    def _read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_lines.append(line)

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    out_thread = threading.Thread(target=_read_stdout, daemon=True)
    err_thread = threading.Thread(target=_read_stderr, daemon=True)
    out_thread.start()
    err_thread.start()
    timed_out = False
    try:
        assert proc.stdin is not None
        proc.stdin.write(stdin_payload)
        proc.stdin.flush()
        proc.stdin.close()
        out_thread.join(timeout=timeout_seconds)
        if out_thread.is_alive():
            timed_out = True
            _kill_process_group(proc)
            out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        result.exit_code = int(proc.wait(timeout=10))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        proc.wait(timeout=5)
        result.exit_code = int(proc.returncode or -1)
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
            proc.wait(timeout=5)
        result.stdout_lines = list(stdout_lines)
        result.stderr_text = ''.join(stderr_chunks)
        result.timed_out = timed_out
    return result


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    except Exception:
        proc.kill()
