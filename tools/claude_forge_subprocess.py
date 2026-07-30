"""Unified subprocess runner for Claude forge spike harness."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
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
    """Run a subprocess under one monotonic deadline, including pipe readers and wait."""
    deadline = time.monotonic() + timeout_seconds
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

    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    try:
        assert proc.stdin is not None
        proc.stdin.write(stdin_payload)
        proc.stdin.flush()
        proc.stdin.close()
        remaining = _remaining()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, timeout_seconds)
        result.exit_code = int(proc.wait(timeout=remaining))

        for reader in (out_thread, err_thread):
            remaining = _remaining()
            if remaining <= 0:
                timed_out = True
                break
            reader.join(timeout=remaining)
        if out_thread.is_alive() or err_thread.is_alive():
            timed_out = True
            _kill_process_group(proc)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        try:
            proc.wait(timeout=min(1.0, max(0.1, timeout_seconds)))
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
        result.exit_code = int(proc.returncode if proc.returncode is not None else -1)
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=min(1.0, max(0.1, timeout_seconds)))
            except subprocess.TimeoutExpired:
                pass
        out_thread.join(timeout=0.1)
        err_thread.join(timeout=0.1)
        if not out_thread.is_alive() and proc.stdout is not None:
            proc.stdout.close()
        if not err_thread.is_alive() and proc.stderr is not None:
            proc.stderr.close()
        result.stdout_lines = list(stdout_lines)
        result.stderr_text = ''.join(stderr_chunks)
        result.timed_out = timed_out
    return result


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    if os.name == 'nt':
        try:
            subprocess.run(
                ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                capture_output=True,
                check=False,
                timeout=0.5,
            )
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    except Exception:
        proc.kill()
