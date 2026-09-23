"""Atomic, credential-free state for managed Claude Code runtime lifecycle."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from chat.cc_runtime import (
    MINIMUM_CLAUDE_CODE_VERSION,
    active_claude_version,
    runtime_state_dir,
    version_tuple,
)

_ALLOWED_CHANNELS = frozenset({'latest', 'stable'})
_PUBLIC_STATE_KEYS = frozenset({
    'status', 'last_check_at', 'last_promoted_at', 'last_error',
    'from', 'to', 'channel', 'canary', 'checked_at', 'promoted_at',
})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _secure_state_dir() -> Path:
    directory = runtime_state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(prefix='.%s.' % path.name, dir=str(directory))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        dir_fd = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_version(name: str, version: Optional[str]) -> None:
    if name not in {'active-version', 'last-good-version', 'candidate-version'}:
        raise ValueError('invalid version state filename')
    value = str(version or '').strip()
    if value:
        version_tuple(value)
    atomic_write(_secure_state_dir() / name, (value + '\n').encode('ascii'))


def read_version(name: str) -> Optional[str]:
    if name not in {'active-version', 'last-good-version', 'candidate-version'}:
        raise ValueError('invalid version state filename')
    try:
        value = (runtime_state_dir() / name).read_text(encoding='ascii').strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError('%s is unreadable' % name) from exc
    if not value:
        return None
    version_tuple(value)
    return value


def read_public_update_state() -> dict[str, Any]:
    path = runtime_state_dir() / 'update-state.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return {'status': 'error', 'last_error': 'runtime_state_unreadable'}
    if not isinstance(raw, dict):
        return {'status': 'error', 'last_error': 'runtime_state_invalid'}
    return {
        key: str(raw[key])[:500] if raw.get(key) is not None else None
        for key in _PUBLIC_STATE_KEYS
        if key in raw
    }


def write_update_state(state: dict[str, Any]) -> None:
    current = read_public_update_state()
    safe: dict[str, Any] = dict(current)
    for key in _PUBLIC_STATE_KEYS:
        if key in state:
            value = state[key]
            safe[key] = None if value is None else str(value)[:500]
    payload = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    atomic_write(_secure_state_dir() / 'update-state.json', payload)


def read_rejected_versions() -> dict[str, Any]:
    path = runtime_state_dir() / 'rejected-versions.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        raise RuntimeError('rejected runtime state is unreadable')
    if not isinstance(raw, dict):
        raise RuntimeError('rejected runtime state is invalid')
    safe: dict[str, Any] = {}
    for version, entry in raw.items():
        try:
            version_tuple(version)
        except Exception:
            continue
        if not isinstance(entry, dict):
            continue
        safe[version] = {
            'reason': str(entry.get('reason') or 'candidate_rejected')[:120],
            'first_seen': str(entry.get('first_seen') or '')[:80],
            'last_seen': str(entry.get('last_seen') or '')[:80],
        }
    return safe


def reject_version(version: str, reason: str) -> None:
    version_tuple(version)
    rejected = read_rejected_versions()
    now = utc_now_iso()
    previous = rejected.get(version) or {}
    rejected[version] = {
        'reason': str(reason or 'candidate_rejected')[:120],
        'first_seen': previous.get('first_seen') or now,
        'last_seen': now,
    }
    payload = json.dumps(rejected, sort_keys=True, separators=(',', ':')).encode('utf-8')
    atomic_write(_secure_state_dir() / 'rejected-versions.json', payload)


def forget_rejected_version(version: str) -> bool:
    version_tuple(version)
    rejected = read_rejected_versions()
    if version not in rejected:
        return False
    del rejected[version]
    atomic_write(
        _secure_state_dir() / 'rejected-versions.json',
        json.dumps(rejected, sort_keys=True, separators=(',', ':')).encode('utf-8'),
    )
    return True


def promote_candidate(
    candidate: str,
    *,
    channel: str,
    checked_at: Optional[str] = None,
    promoted_at: Optional[str] = None,
) -> dict[str, str]:
    version_tuple(candidate)
    if version_tuple(candidate) < version_tuple(MINIMUM_CLAUDE_CODE_VERSION):
        raise ValueError('candidate is below minimum supported version')
    if channel not in _ALLOWED_CHANNELS:
        raise ValueError('invalid update channel')
    previous = active_claude_version()
    if version_tuple(candidate) <= version_tuple(previous):
        raise ValueError('candidate must be newer than active runtime')
    checked = checked_at or utc_now_iso()
    promoted = promoted_at or utc_now_iso()
    # active-version is the sole authority pointer. All supporting receipts
    # are durable before this atomic replacement; a crash can never lose the
    # previous binary or select a partially written version.
    write_version('last-good-version', previous)
    write_version('candidate-version', candidate)
    write_update_state({
        'status': 'promoting',
        'from': previous,
        'to': candidate,
        'channel': channel,
        'checked_at': checked,
        'last_check_at': checked,
        'canary': 'pass',
    })
    write_version('active-version', candidate)
    write_version('candidate-version', None)
    write_update_state({
        'status': 'healthy',
        'from': previous,
        'to': candidate,
        'channel': channel,
        'checked_at': checked,
        'promoted_at': promoted,
        'last_check_at': checked,
        'last_promoted_at': promoted,
        'canary': 'pass',
        'last_error': None,
    })
    return {'from': previous, 'to': candidate, 'checked_at': checked, 'promoted_at': promoted}


def rollback_to_last_good(*, reason: str) -> Optional[str]:
    current = active_claude_version()
    last_good = read_version('last-good-version')
    if not last_good or last_good == current:
        write_update_state({'status': 'error', 'last_error': 'last_good_runtime_unavailable'})
        return None
    reject_version(current, reason)
    write_update_state({
        'status': 'rolled_back',
        'from': current,
        'to': last_good,
        'last_error': str(reason or 'runtime_unhealthy')[:120],
        'canary': 'fail',
    })
    write_version('active-version', last_good)
    write_version('candidate-version', None)
    return last_good


def runtime_public_status(*, auto_update: bool, channel: str) -> dict[str, Any]:
    from chat.cc_runtime import active_claude_binary, runtime_status_dict

    runtime = runtime_status_dict()
    state = read_public_update_state()
    try:
        last_good = read_version('last-good-version')
        candidate = read_version('candidate-version')
    except Exception:
        last_good, candidate = None, None
    if candidate:
        status = str(state.get('status') or 'candidate')
    elif runtime.get('status') == 'healthy':
        status = str(state.get('status') or 'healthy')
    elif runtime.get('status') == 'uninitialized':
        status = 'uninitialized'
    else:
        status = 'error'
    # Deliberately do not return service HOME, binary paths, environment,
    # Claude settings, or any credential-bearing file contents.
    return {
        'active_version': runtime.get('active_version'),
        'last_good_version': last_good,
        'candidate_version': candidate,
        'channel': channel,
        'auto_update': bool(auto_update),
        'status': status,
        'last_check_at': state.get('last_check_at'),
        'last_promoted_at': state.get('last_promoted_at'),
        'last_error': state.get('last_error'),
        'minimum_version': MINIMUM_CLAUDE_CODE_VERSION,
    }
