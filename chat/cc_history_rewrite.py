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
# Legacy split-file idempotency store (pre-unified-state). Read-only migration.
_IDEMPOTENCY_ENV = 'CC_HISTORY_REWRITE_IDEMPOTENCY_PATH'
_IDEMPOTENCY_NAME = 'hayagarden-cc-history-rewrite.idempotency.json'
_UNREADABLE_EPOCH = '__unreadable__'


def _lock_path():
    return os.environ.get(_LOCK_ENV) or os.path.join(tempfile.gettempdir(), _LOCK_NAME)


def _state_path():
    return (
        os.environ.get(_EPOCH_ENV)
        or os.environ.get(_EPOCH_ENV_LEGACY)
        or os.path.join(tempfile.gettempdir(), _EPOCH_NAME)
    )


def _legacy_idempotency_path():
    return (
        os.environ.get(_IDEMPOTENCY_ENV)
        or os.path.join(tempfile.gettempdir(), _IDEMPOTENCY_NAME)
    )


def _empty_state():
    return {
        'epoch': '',
        'reason': 'history_rewrite',
        'idempotency_index': {},
    }


def _unreadable_state():
    return {
        'epoch': _UNREADABLE_EPOCH,
        'reason': 'history_rewrite',
        'idempotency_index': {},
    }


def _read_legacy_idempotency_index():
    """Best-effort migration from the short-lived split idempotency file."""
    path = _legacy_idempotency_path()
    try:
        with open(path, 'rb') as handle:
            raw = handle.read().decode('utf-8', 'ignore').strip()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        epoch = str(entry.get('epoch') or '').strip()
        if epoch:
            out[str(key)] = {'epoch': epoch, 'ts': entry.get('ts')}
    return out


def _normalize_state(data):
    """Ensure idempotency_index exists and backfill the current key if needed.

    Covers legacy single-record files and the brief split-file window where
    the global epoch advanced but a separate idempotency index write failed.
    """
    if not isinstance(data, dict):
        return _unreadable_state()
    epoch = str(data.get('epoch') or '').strip()
    if not epoch:
        return _empty_state()
    reason = str(data.get('reason') or 'history_rewrite')[:80] or 'history_rewrite'
    index = data.get('idempotency_index')
    if not isinstance(index, dict):
        index = {}
    else:
        index = dict(index)
    legacy = _read_legacy_idempotency_index()
    if legacy is None:
        return _unreadable_state()
    for key, entry in legacy.items():
        if key not in index and isinstance(entry, dict):
            epoch_val = str(entry.get('epoch') or '').strip()
            if epoch_val:
                index[key] = {'epoch': epoch_val, 'ts': entry.get('ts')}
    key = data.get('idempotency_key')
    if key:
        key = str(key)
        if key not in index and epoch != _UNREADABLE_EPOCH:
            index[key] = {'epoch': epoch, 'ts': data.get('ts', time.time())}
    return {
        'epoch': epoch,
        'reason': reason,
        'ts': data.get('ts'),
        'idempotency_key': str(key) if key else None,
        'idempotency_index': index,
    }


def _read_durable_state():
    path = _state_path()
    try:
        with open(path, 'rb') as handle:
            raw = handle.read().decode('utf-8', 'ignore').strip()
    except FileNotFoundError:
        return None
    except OSError:
        return _unreadable_state()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        # Legacy single-line reason barrier → treat as opaque epoch token.
        return _normalize_state({'epoch': raw, 'reason': raw[:80]})
    if not isinstance(data, dict):
        return _normalize_state({'epoch': str(data), 'reason': 'history_rewrite'})
    if not str(data.get('epoch') or '').strip():
        return _unreadable_state()
    return _normalize_state(data)


def _atomic_write_durable_state(state):
    path = _state_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = (json.dumps(state, separators=(',', ':'), ensure_ascii=True) + '\n').encode('utf-8')
    tmp = path + '.tmp'
    with open(tmp, 'wb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _lookup_idempotency_epoch(state, idempotency_key):
    key = str(idempotency_key or '').strip()
    if not key or not isinstance(state, dict):
        return ''
    index = state.get('idempotency_index') or {}
    entry = index.get(key)
    if not isinstance(entry, dict):
        return ''
    return str(entry.get('epoch') or '').strip()


def _read_epoch_record():
    state = _read_durable_state()
    if not state:
        return None
    epoch = str(state.get('epoch') or '').strip()
    if not epoch:
        return None
    record = {
        'epoch': epoch,
        'reason': state.get('reason') or 'history_rewrite',
    }
    key = state.get('idempotency_key')
    if key:
        record['idempotency_key'] = str(key)
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


def note_durable_history_rewrite_with_meta(reason='history_rewrite', idempotency_key=None):
    """Like ``note_durable_history_rewrite`` but also reports whether a new epoch
    was minted or an existing idempotency record was reused."""
    key = str(idempotency_key or '').strip() or None
    state = _read_durable_state()
    if state and str(state.get('epoch') or '') == _UNREADABLE_EPOCH:
        return {'epoch': _UNREADABLE_EPOCH, 'advanced': False, 'reused': False}

    if key is not None:
        if state:
            stored_epoch = _lookup_idempotency_epoch(state, key)
            if stored_epoch:
                return {'epoch': stored_epoch, 'advanced': False, 'reused': True}

    epoch = secrets.token_hex(16)
    index = dict((state or {}).get('idempotency_index') or {})
    if key is not None:
        if key not in index:
            index[key] = {'epoch': epoch, 'ts': time.time()}
    new_state = {
        'epoch': epoch,
        'reason': str(reason or 'history_rewrite')[:80] or 'history_rewrite',
        'ts': time.time(),
        'idempotency_index': index,
    }
    if key is not None:
        new_state['idempotency_key'] = key
    _atomic_write_durable_state(new_state)
    return {'epoch': epoch, 'advanced': True, 'reused': False}


def note_durable_history_rewrite(reason='history_rewrite', idempotency_key=None):
    """Advance the durable rewrite epoch after a committed history rewrite.

    Each call produces a unique epoch token and returns it (unchanged
    contract — always a plain epoch string, never a tuple/dict). Reason is
    observational only and must never be used as the version identity. The
    epoch is never cleared by a single worker's invalidate/cold success —
    workers catch up lazily by binding their resident to the latest epoch
    after a successful spawn.

    ``idempotency_key`` (optional, e.g. ``"rewrite:<rewrite_id>"``): when
    given, a durable per-key record is consulted first. If that key already
    owns an epoch (even when a newer rewrite has since advanced the global
    epoch), this call returns the *existing* epoch instead of minting a new
    one. The current epoch, reason, and the full idempotency index live in
    one durable state file and are committed atomically (temp + fsync +
    os.replace) so there is no split-brain between a global epoch advance and
    its per-rewrite idempotency record.

    Callers that never pass ``idempotency_key`` (branch_switch, delete,
    legacy edit paths) keep the original behavior exactly: every call mints
    a brand-new epoch, because each such call represents a genuinely new
    authoritative mutation.
    """
    return note_durable_history_rewrite_with_meta(
        reason, idempotency_key=idempotency_key,
    )['epoch']


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
