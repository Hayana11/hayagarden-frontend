"""Serialize authoritative chat-history rewrites against CC generations."""

from __future__ import annotations

import contextlib
import functools
import os
import tempfile


_LOCK_ENV = 'CC_HISTORY_REWRITE_LOCK_PATH'
_LOCK_NAME = 'hayagarden-cc-history-rewrite.lock'
_BARRIER_ENV = 'CC_HISTORY_REWRITE_BARRIER_PATH'
_BARRIER_NAME = 'hayagarden-cc-history-rewrite.barrier'


def _lock_path():
    return os.environ.get(_LOCK_ENV) or os.path.join(tempfile.gettempdir(), _LOCK_NAME)


def _barrier_path():
    return os.environ.get(_BARRIER_ENV) or os.path.join(tempfile.gettempdir(), _BARRIER_NAME)


def note_durable_history_rewrite(reason='history_rewrite'):
    """Mark that a history rewrite is durable and old hot reuse is forbidden.

    Written after DB commit and before / regardless of app→gateway invalidation.
    Generation paths must treat a present barrier as fail-closed (no hot reuse).
    """
    path = _barrier_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = (str(reason or 'history_rewrite')[:80] or 'history_rewrite').encode('utf-8')
    tmp = path + '.tmp'
    with open(tmp, 'wb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def clear_history_rewrite_barrier():
    """Clear the durable rewrite barrier after authoritative invalidation."""
    path = _barrier_path()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def history_rewrite_barrier_reason():
    """Return pending rewrite reason, or None when hot reuse is still allowed."""
    path = _barrier_path()
    try:
        with open(path, 'rb') as handle:
            text = handle.read().decode('utf-8', 'ignore').strip()
    except FileNotFoundError:
        return None
    except OSError:
        # Unreadable barrier must not fail-open into stale hot reuse.
        return 'history_rewrite'
    return text or 'history_rewrite'


@contextlib.contextmanager
def _cross_process_lock(*, exclusive):
    path = _lock_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            # Windows has no shared msvcrt byte-range lock. Exclusive locking
            # preserves correctness for tests; production Linux uses flock
            # shared locks so normal generations are not serialized here.
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), mode)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def serialize_history_rewrite(func):
    """Hold the rewrite side of the cross-process guard for one route."""

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        with _cross_process_lock(exclusive=True):
            return func(*args, **kwargs)

    return wrapped


def guard_cc_generation(events):
    """Prevent a committed history rewrite from racing a CC generation."""

    with _cross_process_lock(exclusive=False):
        yield from events
