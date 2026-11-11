"""Start-up warm-up of the process-local Continuity and memory caches.

After a gateway (re)start the first hot turn meets empty caches: every source row
of the window is derived from scratch (JSON parsing, token estimates) and every
READY chunk is re-validated, which is most of the time spent loading compressed
blocks. The caches are per process and content-addressed, so the work can be done
ahead of that turn by reading exactly what the turn will read.

Strictly read-only. Nothing here writes a table, advances a cursor, touches
resident state, calls a provider or starts a Claude process: every database is
opened ``mode=ro`` (which also makes the canonical-window resolver's
bootstrap-on-empty-chat write fail instead of happening), and the only side effect
is filling in-process caches. It runs once per process, in a daemon thread after a
delay that keeps it clear of the deployment health check, and a failure costs one
WARNING and nothing else.

Switch: ``CONTINUITY_PREWARM_ENABLED`` in config_store (default on).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

SWITCH_KEY = 'CONTINUITY_PREWARM_ENABLED'
DELAY_SECONDS = 20.0
THREAD_NAME = 'continuity-prewarm'

_lock = threading.Lock()
_started_pid: Optional[int] = None
_requested_db_path: Optional[str] = None
_fork_hook_registered = False
_cancel = threading.Event()


def enabled() -> bool:
    """The runtime switch; read on every call so it can be turned off live."""
    try:
        import config_store
        return bool(config_store.get_bool(SWITCH_KEY, True))
    except Exception:
        return False


def running_under_gunicorn() -> bool:
    """True only inside a gunicorn-managed process (its Arbiter exports this)."""
    return os.environ.get('SERVER_SOFTWARE', '').lower().startswith('gunicorn')


def start(
    db_path: Optional[str] = None,
    *,
    delay_seconds: float = DELAY_SECONDS,
    require_gunicorn: bool = True,
) -> bool:
    """Start the one-shot warm-up thread for this process. True if it was started.

    Tests, scripts and ``python gateway.py`` do not run under gunicorn and so never
    start it. With ``--preload`` the module is imported in the master, whose threads
    do not survive fork: the registered fork hook starts it again in each worker.
    """
    global _requested_db_path
    _register_fork_hook()
    if require_gunicorn and not running_under_gunicorn():
        return False
    if db_path is not None:
        _requested_db_path = str(db_path)
    return _start_thread(_requested_db_path, delay_seconds)


def _start_thread(db_path: Optional[str], delay_seconds: float) -> bool:
    global _started_pid
    if not enabled():
        logger.info('[PREWARM] skipped: %s is off', SWITCH_KEY)
        return False
    with _lock:
        if _started_pid == os.getpid():
            return False  # one warm-up per process
        _started_pid = os.getpid()
        thread = threading.Thread(
            target=_run, args=(db_path, delay_seconds), name=THREAD_NAME, daemon=True,
        )
    thread.start()
    return True


def _register_fork_hook() -> None:
    global _fork_hook_registered
    with _lock:
        if _fork_hook_registered or not hasattr(os, 'register_at_fork'):
            return
        _fork_hook_registered = True
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _after_fork_in_child() -> None:
    """A forked worker starts its own warm-up, but only if this process asked for one."""
    if _requested_db_path is None or not running_under_gunicorn():
        return
    try:
        _start_thread(_requested_db_path, DELAY_SECONDS)
    except Exception:
        logger.warning('[PREWARM] could not start after fork', exc_info=True)


def _run(db_path: Optional[str], delay_seconds: float) -> None:
    try:
        if delay_seconds > 0 and _cancel.wait(delay_seconds):
            return
        if _cancel.is_set():
            return
        if not enabled():  # turned off during the delay
            logger.info('[PREWARM] skipped: %s was turned off', SWITCH_KEY)
            return
        warm(db_path)
    except Exception as exc:  # never reaches threading's excepthook
        logger.warning('[PREWARM] aborted: %s: %s', type(exc).__name__, exc)


# ---- the warm-up itself ---------------------------------------------------------

def warm(db_path: Optional[str] = None) -> dict[str, Any]:
    """Run every step once, never raising. Returns the report that is logged.

    Steps are independent: one failing does not stop the others. All failures of a
    run are reported in a single WARNING.
    """
    if db_path is None:
        from chat import daily_context as dc
        db_path = dc.DEFAULT_DB_PATH
    path = str(db_path)
    report: dict[str, Any] = {}
    failures: list[str] = []
    started = time.perf_counter()
    for name, step in (
        ('ready_surface', _step_ready_surface),
        ('source_rows', _step_source_rows),
        ('memory', _step_memory),
    ):
        step_started = time.perf_counter()
        try:
            stats = step(path)
        except Exception as exc:
            stats = {'error': f'{type(exc).__name__}: {exc}'}
            failures.append(f'{name}: {stats["error"]}')
        stats['ms'] = round((time.perf_counter() - step_started) * 1000, 1)
        report[name] = stats
    report['total_ms'] = round((time.perf_counter() - started) * 1000, 1)
    logger.info('[PREWARM] %s', _format(report))
    if failures:
        logger.warning('[PREWARM] %d step(s) failed, not retried: %s', len(failures), '; '.join(failures))
    report['failures'] = failures
    return report


def _format(report: dict[str, Any]) -> str:
    parts = []
    for name in ('ready_surface', 'source_rows', 'memory'):
        stats = report.get(name, {})
        detail = ' '.join(f'{k}={v}' for k, v in stats.items() if k != 'ms')
        parts.append(f'{name}[{stats.get("ms")}ms {detail}]'.replace(' ]', ']'))
    return ' '.join(parts) + f' total={report.get("total_ms")}ms'


def _step_ready_surface(path: str) -> dict[str, Any]:
    """Fill the READY proof / snapshot / schema caches by validating the surface."""
    from continuity import store

    surface = store.read_ready_surface(path)
    return {'status': surface.status, 'blocks': len(tuple(surface.artifacts or ()))}


def _step_source_rows(path: str) -> dict[str, Any]:
    """Fill the source-row derivation cache for the current window's canonical scope."""
    from chat.daily_continuity_shadow import read_canonical_scope_rows
    from chat.window_identity import WindowIdentityUnavailable, read_current_window_identity_conn
    from continuity import store
    from continuity.sources import CHAT_ID, derive_turns_and_events

    conn = store.open_continuity_read_only(path)  # mode=ro
    try:
        try:
            identity = read_current_window_identity_conn(conn, chat_id=CHAT_ID)
        except WindowIdentityUnavailable as exc:
            return {'skipped': str(exc), 'rows': 0}
        read_started = time.perf_counter()
        rows = read_canonical_scope_rows(
            conn,
            context_id=int(identity['context_id']),
            context_epoch=int(identity['context_epoch']),
        )
        read_ms = round((time.perf_counter() - read_started) * 1000, 1)
    finally:
        conn.close()
    derive_started = time.perf_counter()
    turns, events = derive_turns_and_events(rows)
    return {
        'rows': len(rows), 'turns': len(turns), 'events': len(events),
        'read_ms': read_ms,
        'derive_ms': round((time.perf_counter() - derive_started) * 1000, 1),
    }


def _step_memory(path: str) -> dict[str, Any]:
    """Load jieba's dictionary and fill the memory-recall normalisation memo."""
    stats: dict[str, Any] = {}
    started = time.perf_counter()
    import jieba

    jieba.initialize()
    stats['jieba_ms'] = round((time.perf_counter() - started) * 1000, 1)

    from tools import memory_kernel_read as mkr

    posts = Path(path)
    stats['non_memory_norms'] = len(mkr.non_memory_norms(posts))
    stats['memory_items'] = len(mkr.memory_items(posts, mode=mkr.AUTOMATIC))
    return stats


def reset_for_tests() -> None:
    """Forget that a warm-up ran (test isolation only)."""
    global _started_pid, _requested_db_path
    with _lock:
        _started_pid = None
        _requested_db_path = None
    _cancel.clear()
