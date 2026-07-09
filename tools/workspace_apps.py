"""
workspace_apps.py — live apps under /opt/workspace/apps (PR 4).

Gateway-owned runtime records live under .jobs/app_runtime/ (640, not writable
by wsandbox). Proxy only forwards to verified running apps using the upstream
recorded at start time.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from tools import workspace_executor

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(workspace_executor.EXEC_CWD).resolve()
APPS_DIR = WORKSPACE_ROOT / "apps"
RUNTIME_DIR = WORKSPACE_ROOT / ".jobs" / "app_runtime"
PROXY_PREFIX = "/api/gw/workspace/apps"
_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_RESERVED_PORTS = {5050, 5051, 5052, 5055, 5056, 8000, 8080, 8888, 3000, 5173}
_ALLOWED_UPSTREAM_HOSTS = {"127.0.0.1", "localhost", "::1"}
_RUNTIME_OWNER = "gateway"


class WorkspaceAppError(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sandbox_uid() -> int | None:
    try:
        import pwd
        return pwd.getpwnam(workspace_executor.EXEC_USER).pw_uid
    except (KeyError, ImportError):
        return None


def _harden_path(path: Path, *, is_dir: bool | None = None) -> None:
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


def ensure_apps_dir() -> None:
    old_umask = os.umask(0o007)
    try:
        APPS_DIR.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    _harden_path(APPS_DIR, is_dir=True)


def ensure_runtime_dir() -> None:
    old_umask = os.umask(0o077)
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    try:
        os.chmod(RUNTIME_DIR, 0o2770)
        shutil.chown(
            RUNTIME_DIR,
            user=workspace_executor.EXEC_USER,
            group=workspace_executor.EXEC_GROUP,
        )
    except Exception:
        logger.warning("failed to harden runtime dir %s", RUNTIME_DIR, exc_info=True)


def validate_app_id(app_id: str) -> str:
    app_id = (app_id or "").strip()
    if not _APP_ID_RE.match(app_id):
        raise WorkspaceAppError(
            "invalid_app_id",
            "app id must match ^[a-z][a-z0-9_-]{1,63}$",
        )
    return app_id


def app_dir(app_id: str) -> Path:
    app_id = validate_app_id(app_id)
    root = APPS_DIR.resolve(strict=False)
    path = (root / app_id).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WorkspaceAppError("invalid_app_path", "app path escapes /opt/workspace/apps") from exc
    return path


def runtime_path(app_id: str) -> Path:
    validate_app_id(app_id)
    return RUNTIME_DIR / f"{app_id}.json"


def runtime_lock_path(app_id: str) -> Path:
    validate_app_id(app_id)
    return RUNTIME_DIR / f"{app_id}.lock"


def runtime_log_path(app_id: str) -> Path:
    return app_dir(app_id) / ".runtime.log"


@contextmanager
def _with_app_lock(app_id: str):
    ensure_runtime_dir()
    old_umask = os.umask(0o077)
    try:
        lock_fh = open(runtime_lock_path(app_id), "w", encoding="utf-8")
    finally:
        os.umask(old_umask)
    try:
        os.chmod(runtime_lock_path(app_id), 0o640)
    except Exception:
        pass
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fh.close()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise WorkspaceAppError("invalid_json", f"failed to read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceAppError("invalid_json", f"{path.name} must contain a JSON object")
    return data


def _write_secure_runtime(path: Path, data: dict[str, Any]) -> None:
    """Gateway-owned runtime file: 640 so wsandbox cannot tamper with pid/port."""
    ensure_runtime_dir()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    old_umask = os.umask(0o077)
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp_path, 0o640)
        tmp_path.replace(path)
    finally:
        os.umask(old_umask)


def load_manifest(app_id: str) -> dict[str, Any]:
    path = app_dir(app_id)
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise WorkspaceAppError("app_not_found", f"workspace app not found: {app_id}", 404)
    manifest = _read_json(manifest_path)
    manifest_id = str(manifest.get("id") or app_id).strip()
    if manifest_id != app_id:
        raise WorkspaceAppError("manifest_id_mismatch", "manifest id must match directory name")
    manifest["id"] = app_id
    return manifest


def read_runtime(app_id: str) -> dict[str, Any]:
    path = runtime_path(app_id)
    if not path.is_file():
        return {}
    try:
        return _read_json(path)
    except WorkspaceAppError:
        return {"status": "invalid_runtime"}


def _port_from_manifest(manifest: dict[str, Any]) -> int:
    raw_port = manifest.get("port")
    upstream = str(manifest.get("upstream") or "").strip()
    if raw_port is None and upstream:
        parsed = urlparse(upstream)
        raw_port = parsed.port
    try:
        port = int(raw_port)
    except Exception as exc:
        raise WorkspaceAppError("port_required", "manifest must define a local port") from exc
    if port < 1024 or port > 65535 or port in _RESERVED_PORTS:
        raise WorkspaceAppError(
            "invalid_port",
            "port must be 1024-65535 and cannot be a reserved service port",
        )
    return port


def upstream_base(manifest: dict[str, Any]) -> str:
    port = _port_from_manifest(manifest)
    upstream = str(manifest.get("upstream") or "").strip()
    if not upstream:
        return f"http://127.0.0.1:{port}"
    parsed = urlparse(upstream)
    if parsed.scheme != "http":
        raise WorkspaceAppError("invalid_upstream", "upstream must be http:// loopback (https not supported)")
    if parsed.hostname not in _ALLOWED_UPSTREAM_HOSTS:
        raise WorkspaceAppError("invalid_upstream", "upstream host must be local loopback")
    if parsed.port != port:
        raise WorkspaceAppError("invalid_upstream", "upstream port must match manifest port")
    base = f"http://{parsed.hostname}:{port}"
    if parsed.path and parsed.path != "/":
        base += parsed.path.rstrip("/")
    return base


def proxy_url_for(app_id: str, entry: str | None = None) -> str:
    entry = entry if entry is not None else str(load_manifest(app_id).get("entry") or "/")
    if not entry.startswith("/"):
        entry = "/" + entry
    return f"{PROXY_PREFIX}/{app_id}/proxy{entry}"


def app_public_summary(app_id: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or load_manifest(app_id)
    entry = str(manifest.get("entry") or "/")
    return {
        "id": app_id,
        "name": str(manifest.get("name") or app_id),
        "entry": entry,
        "proxy_url": proxy_url_for(app_id, entry),
        "start_configured": bool(str(manifest.get("start") or "").strip()),
        "autostart": bool(manifest.get("autostart", False)),
        "port": _port_from_manifest(manifest),
    }


def process_state(pid: Any) -> str | None:
    try:
        pid_int = int(pid)
        stat = Path(f"/proc/{pid_int}/stat").read_text(encoding="utf-8", errors="replace")
        state = stat.split()[2]
        if state == "Z":
            return None
        return state
    except Exception:
        return None


def _proc_uid(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1])
    return None


def _proc_pgid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(stat.split()[4])
    except Exception:
        return None


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None


def _verify_runtime(runtime: dict[str, Any], app_id: str) -> bool:
    """Accept only gateway-written runtime with a live wsandbox process in app dir."""
    if not runtime:
        return False
    if runtime.get("owner") != _RUNTIME_OWNER:
        return False
    if runtime.get("id") != app_id:
        return False
    nonce = str(runtime.get("runtime_nonce") or "")
    if not nonce or len(nonce) < 8:
        return False
    pid = runtime.get("pid")
    if pid is None or process_state(pid) is None:
        return False
    pid_int = int(pid)
    expected_uid = _sandbox_uid()
    if expected_uid is not None:
        proc_uid = _proc_uid(pid_int)
        if proc_uid != expected_uid:
            return False
    expected_pgid = runtime.get("pgid")
    proc_pgid = _proc_pgid(pid_int)
    if expected_pgid is not None and proc_pgid is not None and int(expected_pgid) != proc_pgid:
        return False
    app_path = str(app_dir(app_id))
    cwd = _proc_cwd(pid_int)
    if cwd is None or not cwd.startswith(app_path):
        return False
    expected_port = runtime.get("port")
    upstream = str(runtime.get("upstream") or "")
    if expected_port is None or not upstream.startswith("http://"):
        return False
    try:
        parsed = urlparse(upstream)
        if parsed.hostname not in _ALLOWED_UPSTREAM_HOSTS:
            return False
        if parsed.port != int(expected_port):
            return False
    except Exception:
        return False
    return True


def verified_running(runtime: dict[str, Any], app_id: str) -> bool:
    return _verify_runtime(runtime, app_id)


def verified_proxy_upstream(app_id: str) -> str:
    runtime = read_runtime(app_id)
    if not verified_running(runtime, app_id):
        raise WorkspaceAppError(
            "app_not_running",
            "workspace app is not running (proxy requires verified gateway runtime)",
            503,
        )
    return str(runtime["upstream"])


def list_apps() -> list[dict[str, Any]]:
    ensure_apps_dir()
    apps: list[dict[str, Any]] = []
    if not APPS_DIR.exists():
        return apps
    for child in sorted(APPS_DIR.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not _APP_ID_RE.match(child.name):
            continue
        try:
            manifest = load_manifest(child.name)
            summary = app_public_summary(child.name, manifest)
            runtime = read_runtime(child.name)
            running = verified_running(runtime, child.name)
            summary["runtime_status"] = "running" if running else "stopped"
            summary["pid"] = runtime.get("pid") if running else None
            apps.append(summary)
        except Exception as exc:
            apps.append({
                "id": child.name,
                "error": type(exc).__name__,
                "detail": str(exc)[:240],
            })
    return apps


def _render_start_command(manifest: dict[str, Any], app_path: Path) -> str:
    start = str(manifest.get("start") or "").strip()
    if not start:
        raise WorkspaceAppError("start_missing", "manifest start command is required")
    port = str(_port_from_manifest(manifest))
    return (
        start.replace("${PORT}", port)
        .replace("${APP_DIR}", str(app_path))
        .replace("${WORKSPACE}", str(WORKSPACE_ROOT))
    )


def _popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"extra_groups": []}
    try:
        import grp
        import pwd
        pwd.getpwnam(workspace_executor.EXEC_USER)
        grp.getgrnam(workspace_executor.EXEC_GROUP)
        kwargs["user"] = workspace_executor.EXEC_USER
        kwargs["group"] = workspace_executor.EXEC_GROUP
    except (KeyError, LookupError):
        logger.debug("sandbox user/group missing; starting app without privilege drop")
    return kwargs


def _start_app_unlocked(app_id: str, manifest: dict[str, Any], path: Path) -> dict[str, Any]:
    command = _render_start_command(manifest, path)
    log_path = runtime_log_path(app_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _harden_path(log_path.parent, is_dir=True)
    log_file = log_path.open("ab")
    log_file.write((f"\n[{_now_iso()}] $ {command}\n").encode("utf-8", errors="replace"))
    log_file.flush()

    port = _port_from_manifest(manifest)
    env = dict(workspace_executor.EXEC_ENV)
    env.update({
        "PORT": str(port),
        "WORKSPACE": str(WORKSPACE_ROOT),
        "WORKSPACE_APP_ID": app_id,
        "WORKSPACE_APP_DIR": str(path),
    })
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", f"umask 007; {command}"],
            cwd=str(path),
            env=env,
            start_new_session=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **_popen_kwargs(),
        )
    except Exception as exc:
        raise WorkspaceAppError("app_start_failed", str(exc)[:300], 500) from exc
    finally:
        try:
            log_file.close()
        except Exception:
            pass
    _harden_path(log_path, is_dir=False)

    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = proc.pid

    upstream = upstream_base(manifest)
    runtime = {
        "owner": _RUNTIME_OWNER,
        "runtime_nonce": secrets.token_hex(16),
        "id": app_id,
        "status": "running",
        "pid": proc.pid,
        "pgid": pgid,
        "started_at": _now_iso(),
        "cmd": command,
        "port": port,
        "upstream": upstream,
        "proxy_url": proxy_url_for(app_id, str(manifest.get("entry") or "/")),
        "log_path": str(log_path),
    }
    _write_secure_runtime(runtime_path(app_id), runtime)
    return runtime


def start_app(app_id: str) -> dict[str, Any]:
    if not workspace_executor.EXEC_ENABLED:
        raise WorkspaceAppError(
            "exec_disabled",
            "workspace app start is not enabled (set EXEC_ENABLED=1)",
            403,
        )
    manifest = load_manifest(app_id)
    path = app_dir(app_id)
    with _with_app_lock(app_id):
        runtime = read_runtime(app_id)
        if verified_running(runtime, app_id):
            runtime["status"] = "running"
            runtime["already_running"] = True
            return runtime
        return _start_app_unlocked(app_id, manifest, path)


def stop_app(app_id: str, *, force: bool = False) -> dict[str, Any]:
    if not workspace_executor.EXEC_ENABLED:
        raise WorkspaceAppError(
            "exec_disabled",
            "workspace app stop is not enabled (set EXEC_ENABLED=1)",
            403,
        )
    load_manifest(app_id)
    with _with_app_lock(app_id):
        runtime = read_runtime(app_id)
        if not verified_running(runtime, app_id):
            stopped = {
                "id": app_id,
                "owner": _RUNTIME_OWNER,
                "status": "stopped",
                "stopped_at": _now_iso(),
            }
            _write_secure_runtime(runtime_path(app_id), stopped)
            return stopped

        pid = int(runtime["pid"])
        pgid = int(runtime.get("pgid") or pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            runtime.update({"status": "stopped", "stopped_at": _now_iso()})
            _write_secure_runtime(runtime_path(app_id), runtime)
            return runtime
        except Exception as exc:
            raise WorkspaceAppError("app_stop_failed", str(exc)[:300], 500) from exc

        runtime.update({"status": "stopping", "stopping_at": _now_iso()})
        _write_secure_runtime(runtime_path(app_id), runtime)
        for _ in range(10):
            time.sleep(0.2)
            if not verified_running(runtime, app_id):
                runtime.update({"status": "stopped", "stopped_at": _now_iso()})
                _write_secure_runtime(runtime_path(app_id), runtime)
                return runtime

        if force:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
            runtime.update({"status": "stopped", "stopped_at": _now_iso(), "force_killed": True})
            _write_secure_runtime(runtime_path(app_id), runtime)
            return runtime

        runtime.update({"status": "stopping", "still_running": True})
        _write_secure_runtime(runtime_path(app_id), runtime)
        return runtime


def _check_health_from_upstream(upstream: str, manifest: dict[str, Any]) -> dict[str, Any]:
    health_path = str(manifest.get("health") or "").strip()
    if not health_path:
        return {}
    if not health_path.startswith("/"):
        health_path = "/" + health_path
    url = upstream.rstrip("/") + health_path
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            code = resp.getcode()
            return {"ok": code is not None and code < 500, "status_code": code}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:180]}


def app_status(app_id: str, *, include_health: bool = True) -> dict[str, Any]:
    manifest = load_manifest(app_id)
    runtime = read_runtime(app_id)
    running = verified_running(runtime, app_id)
    status = app_public_summary(app_id, manifest)
    status.update({
        "runtime": runtime if running else {k: v for k, v in runtime.items() if k != "pid"},
        "running": running,
        "process_state": process_state(runtime.get("pid")) if running else None,
    })
    if not running:
        status["runtime_status"] = "stopped"
        return status

    status["runtime_status"] = "running"
    if include_health:
        health = _check_health_from_upstream(str(runtime.get("upstream") or ""), manifest)
        if health:
            status["health"] = health
    return status


def restart_app(app_id: str, *, force: bool = False) -> dict[str, Any]:
    if not workspace_executor.EXEC_ENABLED:
        raise WorkspaceAppError(
            "exec_disabled",
            "workspace app restart is not enabled (set EXEC_ENABLED=1)",
            403,
        )
    stop_app(app_id, force=force)
    start_app(app_id)
    time.sleep(0.4)
    return app_status(app_id)


def autostart_apps() -> list[dict[str, Any]]:
    if not workspace_executor.EXEC_ENABLED:
        return []
    ensure_apps_dir()
    results: list[dict[str, Any]] = []
    if not APPS_DIR.exists():
        return results
    for child in sorted(APPS_DIR.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not _APP_ID_RE.match(child.name):
            continue
        app_id = child.name
        try:
            manifest = load_manifest(app_id)
            if not bool(manifest.get("autostart", False)):
                continue
            runtime = start_app(app_id)
            results.append({
                "id": app_id,
                "ok": True,
                "pid": runtime.get("pid"),
                "already_running": bool(runtime.get("already_running", False)),
            })
        except Exception as exc:
            results.append({
                "id": app_id,
                "ok": False,
                "error": type(exc).__name__,
                "detail": str(exc)[:240],
            })
    return results


def proxy_target(upstream: str, path: str, query: str) -> str:
    quoted = "/".join(quote(part, safe="") for part in (path or "").split("/"))
    target = upstream.rstrip("/") + "/" + quoted.lstrip("/")
    if path and path.endswith("/") and not target.endswith("/"):
        target += "/"
    if query:
        target += "?" + query
    return target


def workspace_app(arguments: dict[str, Any]) -> str:
    action = str(arguments.get("action") or "list").strip().lower()
    app_id = str(arguments.get("id") or arguments.get("app_id") or "").strip()
    force = bool(arguments.get("force", False))
    try:
        if action == "list":
            return _json({"ok": True, "apps": list_apps()})
        if not app_id:
            return _json({
                "error": "id_required",
                "actions": ["list", "status", "start", "stop", "restart"],
            })
        if action == "status":
            return _json({"ok": True, "status": app_status(app_id)})
        if action == "start":
            runtime = start_app(app_id)
            time.sleep(0.4)
            return _json({"ok": True, "runtime": runtime, "status": app_status(app_id)})
        if action == "stop":
            runtime = stop_app(app_id, force=force)
            return _json({
                "ok": True,
                "runtime": runtime,
                "status": app_status(app_id, include_health=False),
            })
        if action == "restart":
            return _json({"ok": True, "status": restart_app(app_id, force=force)})
        return _json({
            "error": "invalid_action",
            "actions": ["list", "status", "start", "stop", "restart"],
        })
    except WorkspaceAppError as exc:
        return _json({"error": exc.code, "detail": exc.detail, "status_code": exc.status_code})
