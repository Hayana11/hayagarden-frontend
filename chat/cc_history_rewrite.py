"""Serialize authoritative chat-history rewrites against CC generations."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import secrets
import tempfile
import time


_LOCK_ENV = 'CC_HISTORY_REWRITE_LOCK_PATH'
_LOCK_NAME = 'hayagarden-cc-history-rewrite.lock'
_EPOCH_ENV = 'CC_HISTORY_REWRITE_EPOCH_PATH'
# Backward-compatible alias used by earlier barrier-path tests/env.
_EPOCH_ENV_LEGACY = 'CC_HISTORY_REWRITE_BARRIER_PATH'
_EPOCH_NAME = 'hayagarden-cc-history-rewrite.epoch'


def _lock_path():
    return os.environ.get(_LOCK_ENV) or os.path.join(tempfile.gettempdir(), _LOCK_NAME)


def _epoch_path():
    return (
        os.environ.get(_EPOCH_ENV)
        or os.environ.get(_EPOCH_ENV_LEGACY)
        or os.path.join(tempfile.gettempdir(), _EPOCH_NAME)
    )


def _read_epoch_record():
    path = _epoch_path()
    try:
        with open(path, 'rb') as handle:
            raw = handle.read().decode('utf-8', 'ignore').strip()
    except FileNotFoundError:
        return None
    except OSError:
        # Unreadable durable epoch must not fail-open into stale hot reuse.
        return {'epoch': '__unreadable__', 'reason': 'history_rewrite'}
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        # Legacy single-line reason barrier → treat as opaque epoch token.
        return {'epoch': raw, 'reason': raw[:80]}
    if not isinstance(data, dict):
        return {'epoch': str(data), 'reason': 'history_rewrite'}
    epoch = str(data.get('epoch') or '').strip()
    if not epoch:
        return {'epoch': '__unreadable__', 'reason': 'history_rewrite'}
    reason = str(data.get('reason') or 'history_rewrite')[:80] or 'history_rewrite'
    record = {'epoch': epoch, 'reason': reason}
    idempotency_key = data.get('idempotency_key')
    if idempotency_key:
        record['idempotency_key'] = str(idempotency_key)
    return record


def current_history_rewrite_epoch():
    """Return the durable rewrite epoch token, or '' when none exists."""
    record = _read_epoch_record()
    if not record:
        return ''
    return str(record.get('epoch') or '')


def history_rewrite_epoch_reason():
    """Observability helper: reason attached to the durable epoch, or None."""
    record = _read_epoch_record()
    if not record:
        return None
    return record.get('reason') or 'history_rewrite'


# Backward-compatible name used by earlier regressions.
history_rewrite_barrier_reason = history_rewrite_epoch_reason


def note_durable_history_rewrite(reason='history_rewrite', idempotency_key=None):
    """Advance the durable rewrite epoch after a committed history rewrite.

    Each call produces a unique epoch token and returns it (unchanged
    contract — always a plain epoch string, never a tuple/dict). Reason is
    observational only and must never be used as the version identity. The
    epoch is never cleared by a single worker's invalidate/cold success —
    workers catch up lazily by binding their resident to the latest epoch
    after a successful spawn.

    ``idempotency_key`` (optional, e.g. ``"rewrite:<rewrite_id>"``): when
    given and it matches the key already stored on the current epoch record,
    this call is a no-op retry — it returns the *existing* epoch instead of
    minting a new one. This covers effects/replay retries for one committed
    rewrite (same rewrite_id must advance the epoch at most once) as well as
    the crash-recovery case where the epoch file was written but the
    per-rewrite durable marker was not yet persisted by the caller.

    Callers that never pass ``idempotency_key`` (branch_switch, delete,
    legacy edit paths) keep the original behavior exactly: every call mints
    a brand-new epoch, because each such call represents a genuinely new
    authoritative mutation.
    """
    key = str(idempotency_key or '').strip() or None
    if key is not None:
        current = _read_epoch_record()
        if current and current.get('idempotency_key') == key:
            return str(current.get('epoch') or '')
    path = _epoch_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    epoch = secrets.token_hex(16)
    record = {
        'epoch': epoch,
        'reason': str(reason or 'history_rewrite')[:80] or 'history_rewrite',
        'ts': time.time(),
    }
    if key is not None:
        record['idempotency_key'] = key
    payload = (json.dumps(record, separators=(',', ':'), ensure_ascii=True) + '\n').encode('utf-8')
    tmp = path + '.tmp'
    with open(tmp, 'wb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return epoch


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
