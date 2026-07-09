"""
workspace_jobs.py — background jobs in /opt/workspace/.jobs (PR 2).

ws_job start/status/tail/list/stop. Finished jobs with notify=True emit events
through a pluggable hook; gateway registers the hook to queue SSE + persist
chat_messages without acquiring _gen_busy.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import signal
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools import workspace_executor

logger = logging.getLogger(__name__)

JOBS_DIR = Path(workspace_executor.EXEC_CWD) / ".jobs"
EVENTS_DIR = JOBS_DIR / "events"
PENDING_EVENTS_FILE = EVENTS_DIR / "pending.jsonl"
EVENTS_LOCK_FILE = EVENTS_DIR / ".events.lock"
_JOB_ID_RE = re.compile(r"^job_[a-f0-9]{12}$")

_event_hook: Callable[[dict[str, Any]], None] | None = None
_sweep_started = False
_sweep_lock = threading.Lock()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def set_event_hook(hook: Callable[[dict[str, Any]], None] | None) -> None:
    global _event_hook
    _event_hook = hook


def _harden_workspace_path(path: Path, *, is_dir: bool | None = None) -> None:
    """Apply PR1 group-only permissions (2770 dir / 660 file, wsandbox:workspace)."""
    import shutil
    if is_dir is None:
        is_dir = path.is_dir() or not path.exists()
    mode = 0o2770 if is_dir else 0o660
    try:
        if is_dir:
            path.mkdir(parents=True, exist_ok=True)
        if path.exists():
            os.chmod(path, mode)
            shutil.chown(
                path,
                user=workspace_executor.EXEC_USER,
                group=workspace_executor.EXEC_GROUP,
            )
    except Exception:
        logger.warning("failed to harden sandbox path %s", path, exc_info=True)


def _ensure_events_dir() -> None:
    old_umask = os.umask(0o007)
    try:
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    _harden_workspace_path(EVENTS_DIR, is_dir=True)


@contextmanager
def _with_events_lock():
    """Cross-worker file lock for pending SSE event queue."""
    _ensure_events_dir()
    old_umask = os.umask(0o007)
    try:
        lock_fh = open(EVENTS_LOCK_FILE, "w", encoding="utf-8")
    finally:
        os.umask(old_umask)
    _harden_workspace_path(EVENTS_LOCK_FILE, is_dir=False)
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fh.close()


def queue_event(event: dict[str, Any]) -> None:
    """Append a pending delivery event to shared .jobs/events/pending.jsonl."""
    line = _json(event)
    with _with_events_lock():
        old_umask = os.umask(0o007)
        try:
            with PENDING_EVENTS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        finally:
            os.umask(old_umask)
        _harden_workspace_path(PENDING_EVENTS_FILE, is_dir=False)


def drain_pending_events() -> list[dict[str, Any]]:
    """Atomically read and clear all pending delivery events (any worker)."""
    with _with_events_lock():
        if not PENDING_EVENTS_FILE.exists():
            return []
        raw = PENDING_EVENTS_FILE.read_text(encoding="utf-8")
        PENDING_EVENTS_FILE.write_text("", encoding="utf-8")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            logger.warning("skipped corrupt pending event line: %s", line[:200])
    return events


def _emit_event(event: dict[str, Any]) -> None:
    hook = _event_hook
    if hook is None:
        queue_event(event)
        logger.info("workspace job event (no hook): %s", _json(event)[:500])
        return
    try:
        hook(event)
    except Exception:
        logger.warning("workspace job event hook failed", exc_info=True)
        queue_event(event)


def _ensure_jobs_dir() -> None:
    old_umask = os.umask(0o007)
    try:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    _harden_workspace_path(JOBS_DIR, is_dir=True)
    _ensure_events_dir()


def _job_paths(job_id: str) -> tuple[Path, Path, Path]:
    if not _JOB_ID_RE.match(job_id):
        raise ValueError("invalid_job_id")
    return (
        JOBS_DIR / f"{job_id}.json",
        JOBS_DIR / f"{job_id}.log",
        JOBS_DIR / f"{job_id}.py",
    )


def _read_job_meta(job_id: str) -> dict[str, Any]:
    meta_path, _, _ = _job_paths(job_id)
    if not meta_path.exists():
        raise FileNotFoundError(job_id)
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"id": job_id, "error": "meta_read_failed", "detail": str(exc)[:300]}


def _process_state(pid: Any) -> str | None:
    try:
        pid_int = int(pid)
        stat = Path(f"/proc/{pid_int}/stat").read_text(encoding="utf-8", errors="replace")
        return stat.split()[2]
    except Exception:
        return None


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(minimum, min(maximum, number))


def job_log_tail(job_id: str, lines: int = 80) -> str:
    try:
        _, log_path, _ = _job_paths(job_id)
    except ValueError:
        return ""
    if not log_path.exists():
        return ""
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        return "".join(deque(fh, maxlen=max(1, lines)))


def _write_runner(job_id: str, cmd: str, name: str, meta_path: Path, log_path: Path,
                  *, notify: bool = True, conversation_id: str = "") -> Path:
    _, _, runner_path = _job_paths(job_id)
    initial = {
        "id": job_id,
        "name": name,
        "cmd": cmd,
        "status": "queued",
        "created_at": _iso(_now()),
        "log_path": str(log_path),
        "notify": bool(notify),
        "conversation_id": conversation_id or "default",
        "notified": False,
    }
    meta_path.write_text(_json(initial), encoding="utf-8")
    log_path.write_text("", encoding="utf-8")
    root = str(Path(workspace_executor.EXEC_CWD).resolve())
    runner = f'''from __future__ import annotations
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

JOB_ID = {job_id!r}
CMD = {cmd!r}
META_PATH = Path({str(meta_path)!r})
LOG_PATH = Path({str(log_path)!r})
WORKSPACE_ROOT = {root!r}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def write_meta(update):
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {{"id": JOB_ID}}
    data.update(update)
    META_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

os.umask(0o007)
write_meta({{"status": "running", "started_at": now(), "pid": os.getpid()}})
child_env = {{k: os.environ[k] for k in os.environ if k in {sorted(workspace_executor.EXEC_ENV.keys())!r}}}
with LOG_PATH.open("ab") as log:
    log.write(("[" + now() + "] $ " + CMD + "\\n").encode("utf-8", errors="replace"))
    log.flush()
    proc = subprocess.Popen(
        ["/bin/bash", "-lc", "umask 007; " + CMD],
        cwd=WORKSPACE_ROOT,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    assert proc.stdout is not None
    for chunk in proc.stdout:
        log.write(chunk.encode("utf-8", errors="replace"))
        log.flush()
    proc.wait()
write_meta({{
    "status": "succeeded" if proc.returncode == 0 else "failed",
    "exit_code": proc.returncode,
    "finished_at": now(),
}})
'''
    runner_path.write_text(runner, encoding="utf-8")
    runner_path.chmod(0o750)
    for path in (meta_path, log_path, runner_path):
        try:
            import shutil
            shutil.chown(path, user=workspace_executor.EXEC_USER,
                         group=workspace_executor.EXEC_GROUP)
        except Exception:
            logger.warning("failed to chown job file %s", path, exc_info=True)
    return runner_path


def ws_job(arguments: dict[str, Any], conversation_id: str = "") -> str:
    action = str(arguments.get("action") or "status").strip().lower()
    _ensure_jobs_dir()

    if action == "start":
        if not workspace_executor.EXEC_ENABLED:
            return _json({
                "error": "exec_disabled",
                "detail": "background jobs require EXEC_ENABLED=1",
            })
        cmd = str(arguments.get("cmd") or "").strip()
        if not cmd:
            return _json({"error": "cmd_required"})
        blocked = workspace_executor.blocked_reason(cmd)
        if blocked:
            return _json({
                "error": "blocked",
                "detail": f"command matched the slip blocklist: {blocked}",
            })
        name = str(arguments.get("name") or "").strip()[:80]
        notify = bool(arguments.get("notify", True))
        job_id = "job_" + uuid.uuid4().hex[:12]
        meta_path, log_path, _ = _job_paths(job_id)
        runner_path = _write_runner(
            job_id, cmd, name, meta_path, log_path,
            notify=notify, conversation_id=conversation_id,
        )
        env = dict(workspace_executor.EXEC_ENV)
        try:
            proc = __import__("subprocess").Popen(
                ["python3", str(runner_path)],
                cwd=workspace_executor.EXEC_CWD,
                user=workspace_executor.EXEC_USER,
                group=workspace_executor.EXEC_GROUP,
                extra_groups=[],
                env=env,
                start_new_session=True,
                stdout=__import__("subprocess").DEVNULL,
                stderr=__import__("subprocess").DEVNULL,
            )
            threading.Thread(
                target=proc.wait, name=f"ws-job-wait-{job_id}", daemon=True,
            ).start()
        except Exception as exc:
            return _json({"error": "job_start_failed", "detail": str(exc)[:300]})
        return _json({
            "ok": True,
            "id": job_id,
            "pid": proc.pid,
            "status": "started",
            "log_path": str(log_path),
            "notify_on_finish": notify,
        })

    if action == "list":
        jobs: list[dict[str, Any]] = []
        for meta_path in sorted(JOBS_DIR.glob("job_*.json"), reverse=True)[:50]:
            try:
                jobs.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return _json({"ok": True, "jobs": jobs})

    job_id = str(arguments.get("id") or arguments.get("job_id") or "").strip()
    if not job_id:
        return _json({"error": "id_required"})

    try:
        meta_path, log_path, _ = _job_paths(job_id)
        meta = _read_job_meta(job_id)
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)[:300]})

    if action == "status":
        if meta.get("status") == "running":
            state = _process_state(meta.get("pid"))
            if state is None:
                meta["status_hint"] = "process_missing"
            else:
                meta["process_state"] = state
        return _json({"ok": True, "job": meta})

    if action == "tail":
        lines = _coerce_int(arguments.get("lines"), 80, 1, 500)
        tail = job_log_tail(job_id, lines) if log_path.exists() else ""
        return _json({"ok": True, "job": meta, "log": tail})

    if action == "stop":
        pid = meta.get("pid")
        try:
            os.killpg(int(pid), signal.SIGTERM)
            meta["status"] = "stopping"
            meta["stopped_at"] = _iso(_now())
            meta_path.write_text(_json(meta), encoding="utf-8")
            return _json({"ok": True, "job": meta})
        except Exception as exc:
            return _json({"error": "stop_failed", "detail": str(exc)[:300], "job": meta})

    return _json({
        "error": "invalid_action",
        "actions": ["start", "status", "tail", "list", "stop"],
    })


def _emit_job_finished(meta: dict[str, Any]) -> None:
    dur = ""
    try:
        started = datetime.fromisoformat(str(meta.get("started_at")))
        finished = datetime.fromisoformat(str(meta.get("finished_at")))
        dur = f" in {int((finished - started).total_seconds())}s"
    except Exception:
        pass
    label = meta.get("name") or meta.get("id")
    log_tail = job_log_tail(str(meta.get("id") or ""), 40)
    content = (
        f"[ws_job] {label} ({meta.get('id')}) {meta.get('status')}{dur}, "
        f"exit_code={meta.get('exit_code')}. "
        f"Inspect with ws_job action=tail id={meta.get('id')} and continue the work."
    )
    _emit_event({
        "type": "job_finished",
        "conversation_id": str(meta.get("conversation_id") or "default"),
        "content": content,
        "title": "ws_job",
        "preview": f"{label} {meta.get('status')}",
        "log_tail": log_tail,
        "meta": {
            "job_id": meta.get("id"),
            "status": meta.get("status"),
            "exit_code": meta.get("exit_code"),
            "name": meta.get("name"),
        },
        "idempotency_key": f"{meta.get('id')}_done",
    })


def notify_finished_jobs() -> None:
    """Scan finished jobs with notify=True and emit events once."""
    if not JOBS_DIR.exists():
        return
    lock_path = JOBS_DIR / ".notify.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        for meta_path in sorted(JOBS_DIR.glob("job_*.json"), reverse=True)[:50]:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("status") not in ("succeeded", "failed"):
                continue
            if not meta.get("notify") or meta.get("notified"):
                continue
            try:
                _emit_job_finished(meta)
                meta["notified"] = True
                meta["notified_at"] = _iso(_now())
            except Exception as exc:
                logger.warning("job notify failed for %s", meta.get("id"), exc_info=True)
                meta["notified"] = True
                meta["notify_error"] = str(exc)[:300]
            try:
                meta_path.write_text(_json(meta), encoding="utf-8")
            except Exception:
                logger.warning("job meta write-back failed for %s", meta.get("id"), exc_info=True)


def _sweep_loop() -> None:
    while True:
        try:
            notify_finished_jobs()
        except Exception:
            logger.warning("workspace job sweep failed", exc_info=True)
        time.sleep(10)


def start_sweep_thread() -> None:
    global _sweep_started
    with _sweep_lock:
        if _sweep_started:
            return
        _sweep_started = True
    threading.Thread(target=_sweep_loop, name="ws-job-sweep", daemon=True).start()
    logger.info("workspace job sweep thread started")
