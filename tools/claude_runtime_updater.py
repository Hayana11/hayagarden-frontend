"""Bounded Claude Code native updater and candidate promotion worker.

The official updater downloads versions. HayaGarden alone selects the active
version. No command in this module submits a user turn or performs generation.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import selectors
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_LOCK = Path('/var/lock/hayagarden-frontend-deploy.lock')
UPDATE_LOCK = Path('/run/lock/hayagarden-claude-runtime-update.lock')
SEVERE_STDERR_RE = re.compile(r'(?i)\b(fatal|uncaught exception|failed to initialize)\b')


def _runtime_imports():
    from chat.cc_runtime import (
        MINIMUM_CLAUDE_CODE_VERSION,
        NATIVE_VERSIONS_RELATIVE,
        active_claude_version,
        service_home,
        version_tuple,
    )
    from chat.claude_runtime_state import (
        forget_rejected_version,
        promote_candidate,
        read_rejected_versions,
        reject_version,
        read_version,
        write_update_state,
    )
    return locals()


@contextmanager
def update_locks():
    """All lifecycle writers take deploy lock first, update lock second."""
    paths = (DEPLOY_LOCK, UPDATE_LOCK)
    handles = []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, 'a+b')
            handles.append(handle)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()


def _prefs():
    import config_store

    enabled = config_store.get('CC_AUTO_UPDATE_ENABLED') or 'true'
    channel = config_store.get('CC_AUTO_UPDATE_CHANNEL') or 'latest'
    enabled_text = str(enabled).strip().lower()
    channel_text = str(channel).strip().lower()
    return enabled_text in {'1', 'true', 'yes', 'on'}, channel_text


def _settings_path() -> Path:
    from chat.cc_runtime import service_home
    return service_home() / '.claude' / 'settings.json'


def sync_native_update_settings(*, enabled: bool, channel: str) -> None:
    """Set only the two updater keys; preserve all OAuth/unrelated settings."""
    if channel not in {'latest', 'stable'}:
        raise ValueError('invalid update channel')
    from chat.claude_runtime_state import atomic_write

    path = _settings_path()
    try:
        original = path.read_bytes()
        raw = json.loads(original.decode('utf-8'))
        if not isinstance(raw, dict):
            raise ValueError('Claude settings must be an object')
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        original = b''
        raw = {}
        mode = 0o600
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError('Claude update settings are unreadable') from exc
    raw['autoUpdates'] = bool(enabled)
    raw['autoUpdatesChannel'] = channel
    payload = (json.dumps(raw, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    atomic_write(path, payload, mode=mode or 0o600)


def _native_updater(home: Path) -> Path:
    launcher = home / '.local' / 'bin' / 'claude'
    try:
        resolved = launcher.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError('native_updater_unavailable') from exc
    versions = (home / '.local' / 'share' / 'claude' / 'versions').resolve()
    if not resolved.is_file() or not os.access(str(resolved), os.X_OK):
        raise RuntimeError('native_updater_unavailable')
    if versions not in resolved.parents:
        raise RuntimeError('native_updater_not_managed')
    return launcher


def discover_downloaded_candidate(*, home: Optional[Path] = None) -> Optional[str]:
    from chat.cc_runtime import (
        MINIMUM_CLAUDE_CODE_VERSION,
        NATIVE_VERSIONS_RELATIVE,
        active_claude_version,
        service_home,
        version_tuple,
        probe_claude_version,
    )
    from chat.claude_runtime_state import read_rejected_versions

    home = home or service_home()
    active = active_claude_version()
    rejected = read_rejected_versions()
    versions_dir = home / NATIVE_VERSIONS_RELATIVE
    try:
        entries = list(versions_dir.iterdir())
    except OSError:
        return None
    available: list[tuple[tuple[int, int, int], str]] = []
    for path in entries:
        name = path.name
        try:
            name_version = version_tuple(name)
        except Exception:
            continue
        if name_version <= version_tuple(active):
            continue
        if name_version < version_tuple(MINIMUM_CLAUDE_CODE_VERSION) or name in rejected:
            continue
        try:
            actual = probe_claude_version(path)
        except Exception:
            continue
        if actual != name or not path.is_file() or not os.access(str(path), os.X_OK):
            continue
        available.append((name_version, name))
    return max(available)[1] if available else None


def _run_candidate_command(binary: Path, args: list[str], *, env: dict[str, str]) -> None:
    try:
        result = subprocess.run(
            [str(binary), *args],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('candidate_command_failed') from exc
    if result.returncode != 0:
        raise RuntimeError('candidate_command_failed')
    if SEVERE_STDERR_RE.search((result.stderr or '')[:4096]):
        raise RuntimeError('candidate_command_failed')


def candidate_surface_canary(binary: Path, *, env: dict[str, str], stable_seconds: float = 1.0) -> bool:
    """Start resident-shaped stream-json pipes and prove startup without sending input."""
    command = [
        str(binary), '-p',
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--include-partial-messages',
        '--max-turns', '5',
        '--tools', '',
        '--thinking-display', 'summarized',
        '--exclude-dynamic-system-prompt-sections',
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    healthy = True
    stderr_sample = bytearray()
    stdout_bytes = 0
    try:
        selector = selectors.DefaultSelector()
        for name, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
            if stream is None:
                healthy = False
                continue
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + max(0.1, float(stable_seconds))
        while healthy and time.monotonic() < deadline:
            if process.poll() is not None:
                healthy = False
                break
            for key, _ in selector.select(timeout=0.025):
                try:
                    sample = os.read(key.fileobj.fileno(), 4096)
                except BlockingIOError:
                    continue
                if not sample:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:
                        pass
                    continue
                if key.data == 'stderr':
                    stderr_sample.extend(sample[:4096 - len(stderr_sample)])
                    if SEVERE_STDERR_RE.search(stderr_sample.decode('utf-8', errors='replace')):
                        healthy = False
                        break
                else:
                    stdout_bytes += len(sample)
                    if stdout_bytes > 65536:
                        healthy = False
                        break
        selector.close()
        return healthy and process.poll() is None
    except (OSError, ValueError):
        return False
    finally:
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=3)
            except Exception:
                pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

def canary_candidate(version: str, *, env: Optional[dict[str, str]] = None) -> bool:
    from chat.cc_runtime import (
        NATIVE_VERSIONS_RELATIVE,
        service_home,
        probe_claude_version,
    )

    runtime_env = dict(os.environ if env is None else env)
    if not str(runtime_env.get('HOME') or '').strip():
        runtime_env['HOME'] = str(service_home(runtime_env))
    binary = service_home(runtime_env) / NATIVE_VERSIONS_RELATIVE / version
    try:
        actual = probe_claude_version(binary, env=runtime_env)
        if actual != version:
            return False
        _run_candidate_command(binary, ['--help'], env=runtime_env)
        _run_candidate_command(binary, ['doctor'], env=runtime_env)
        return candidate_surface_canary(binary, env=runtime_env)
    except Exception:
        return False


def _save_rejection(version: str, reason: str) -> None:
    from chat.claude_runtime_state import reject_version, write_update_state
    reject_version(version, reason)
    write_update_state({
        'status': 'rejected',
        'to': version,
        'canary': 'fail',
        'last_error': reason,
    })


def run_update_check(*, force: bool = False) -> str:
    from chat.cc_runtime import (
        MINIMUM_CLAUDE_CODE_VERSION,
        active_claude_version,
        service_home,
        version_tuple,
    )
    from chat.claude_runtime_state import (
        read_rejected_versions,
        write_update_state,
        promote_candidate,
        forget_rejected_version,
        read_public_update_state,
        read_version,
        write_version,
    )

    enabled, channel = _prefs()
    if channel not in {'latest', 'stable'}:
        write_update_state({'status': 'error', 'last_error': 'invalid_update_channel'})
        return 'invalid_update_channel'
    now = __import__('chat.claude_runtime_state', fromlist=['utc_now_iso']).utc_now_iso()
    write_update_state({'status': 'checking', 'last_check_at': now, 'channel': channel, 'last_error': None})
    sync_native_update_settings(enabled=enabled, channel=channel)
    if not enabled and not force:
        write_update_state({'status': 'disabled', 'last_check_at': now, 'channel': channel})
        return 'disabled'
    active = active_claude_version()
    if version_tuple(active) < version_tuple(MINIMUM_CLAUDE_CODE_VERSION):
        write_update_state({'status': 'error', 'last_error': 'active_runtime_below_minimum'})
        return 'active_runtime_below_minimum'
    home = service_home()
    updater = _native_updater(home)
    command_env = os.environ.copy()
    command_env['HOME'] = str(home)
    try:
        result = subprocess.run(
            [str(updater), 'update'],
            cwd=str(ROOT),
            env=command_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        write_update_state({'status': 'error', 'last_error': 'native_update_failed'})
        return 'native_update_failed'
    if result.returncode != 0:
        write_update_state({'status': 'error', 'last_error': 'native_update_failed'})
        return 'native_update_failed'
    candidate = discover_downloaded_candidate(home=home)
    if candidate is None:
        prior_state = read_public_update_state()
        prior_candidate = read_version('candidate-version')
        if prior_state.get('status') == 'rejected' and prior_candidate:
            write_update_state({'last_check_at': now, 'channel': channel})
            return 'candidate_rejected_previously'
        write_version('candidate-version', None)
        write_update_state({'status': 'up_to_date', 'last_check_at': now, 'channel': channel, 'last_error': None})
        return 'up_to_date'
    write_version('candidate-version', candidate)
    write_update_state({'status': 'candidate', 'last_check_at': now, 'channel': channel, 'to': candidate})
    if candidate in read_rejected_versions() and not force:
        return 'candidate_rejected_previously'
    if not canary_candidate(candidate, env=command_env):
        _save_rejection(candidate, 'startup_canary_failed')
        return 'candidate_rejected'
    promote_candidate(candidate, channel=channel, checked_at=now)
    return 'promoted'


def run_locked_check(*, force: bool = False) -> str:
    with update_locks() as acquired:
        if not acquired:
            return 'skipped_lock_busy'
        return run_update_check(force=force)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='run a scheduled update check')
    parser.add_argument('--manual', action='store_true', help='run an owner-triggered check even when automatic updates are disabled')
    parser.add_argument('--retry-rejected')
    args = parser.parse_args(argv)
    if args.retry_rejected:
        from chat.claude_runtime_state import forget_rejected_version
        try:
            forget_rejected_version(args.retry_rejected)
        except Exception:
            print('CLAUDE_RUNTIME_UPDATE_RESULT=invalid_retry_version')
            return 2
    result = run_locked_check(force=bool(args.manual or args.retry_rejected))
    print('CLAUDE_RUNTIME_UPDATE_RESULT=%s MODEL_GENERATION_REQUESTS=0' % result)
    return 0 if result not in {'native_update_failed', 'candidate_rejected', 'active_runtime_below_minimum', 'invalid_update_channel'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
