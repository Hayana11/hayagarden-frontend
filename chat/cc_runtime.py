"""Managed native Claude Code runtime resolver.

HayaGarden owns the active-version pointer; Anthropic's native installer owns
acquiring versioned binaries. Production callers never fall back to PATH, npm,
or npx. Runtime state contains no authentication material.
"""
from __future__ import annotations

import os
import pwd
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

MINIMUM_CLAUDE_CODE_VERSION = '2.1.280'
RUNTIME_STATE_DIR = Path('/var/lib/hayagarden/claude-runtime')
NATIVE_VERSIONS_RELATIVE = Path('.local/share/claude/versions')

_VERSION_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')
_CACHE_SECONDS = 30.0
_version_cache: dict[str, tuple[str, float]] = {}


class ClaudeRuntimeError(RuntimeError):
    """Managed Claude Code runtime is missing, invalid, or unsupported."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def runtime_state_dir() -> Path:
    return RUNTIME_STATE_DIR


def service_home(env: Optional[dict[str, str]] = None) -> Path:
    source = env if env is not None else os.environ
    value = (source.get('HOME') or '').strip()
    if value:
        return Path(value).expanduser()
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError, AttributeError):
        raise ClaudeRuntimeError('service home is unavailable')


def version_tuple(value: str) -> tuple[int, int, int]:
    text = str(value or '').strip()
    match = _VERSION_RE.fullmatch(text)
    if not match:
        raise ClaudeRuntimeError('invalid Claude Code version')
    return tuple(int(part) for part in match.groups())


def _read_version_file(name: str) -> str:
    path = runtime_state_dir() / name
    try:
        value = path.read_text(encoding='ascii').strip()
    except OSError as exc:
        raise ClaudeRuntimeError('%s is unavailable' % name) from exc
    version_tuple(value)
    return value


def active_claude_version() -> str:
    """Read the sole production version authority; never infer from PATH."""
    return _read_version_file('active-version')


def last_good_claude_version() -> Optional[str]:
    path = runtime_state_dir() / 'last-good-version'
    try:
        value = path.read_text(encoding='ascii').strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ClaudeRuntimeError('last-good-version is unreadable') from exc
    if not value:
        return None
    version_tuple(value)
    return value


def native_claude_binary(version: str, *, env: Optional[dict[str, str]] = None) -> Path:
    version_tuple(version)
    versions_dir = service_home(env) / NATIVE_VERSIONS_RELATIVE
    binary = versions_dir / version
    try:
        resolved_root = versions_dir.resolve(strict=True)
        resolved_binary = binary.resolve(strict=True)
    except OSError as exc:
        raise ClaudeRuntimeError('managed native Claude Code binary is missing') from exc
    if resolved_binary.parent != resolved_root or not resolved_binary.is_file():
        raise ClaudeRuntimeError('managed native Claude Code binary is invalid')
    if not os.access(str(resolved_binary), os.X_OK):
        raise ClaudeRuntimeError('managed native Claude Code binary is not executable')
    return resolved_binary


def active_claude_binary(*, env: Optional[dict[str, str]] = None) -> Path:
    """Resolve active-version to exactly native versions/<version>."""
    return native_claude_binary(active_claude_version(), env=env)


def parse_claude_version_text(text: str) -> str:
    match = re.search(r'(?<!\d)(\d+\.\d+\.\d+)(?!\d)', text or '')
    if not match:
        raise ClaudeRuntimeError('Claude Code version output is invalid')
    value = match.group(1)
    version_tuple(value)
    return value


def _probe_binary(binary: Path, *, env=None, cwd=None, timeout=30.0) -> str:
    key = str(binary)
    now = time.monotonic()
    cached = _version_cache.get(key)
    if cached and (now - cached[1]) < _CACHE_SECONDS:
        return cached[0]
    try:
        proc = subprocess.run(
            [str(binary), '--version'],
            cwd=cwd or str(repo_root()),
            env=env or os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClaudeRuntimeError('Claude Code version probe failed') from exc
    if proc.returncode != 0:
        raise ClaudeRuntimeError('Claude Code version probe failed')
    value = parse_claude_version_text((proc.stdout or '') + '\n' + (proc.stderr or ''))
    _version_cache[key] = (value, now)
    return value


def probe_claude_version(
    binary: Optional[str | Path] = None,
    *,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    timeout: float = 30.0,
) -> str:
    """Probe one exact binary (active native binary by default)."""
    path = Path(binary) if binary is not None else active_claude_binary(env=env)
    return _probe_binary(path, env=env, cwd=cwd, timeout=timeout)


def require_managed_claude_runtime(
    *,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    minimum_version: str = MINIMUM_CLAUDE_CODE_VERSION,
    timeout: float = 30.0,
) -> str:
    """Return the verified active version, failing closed below the minimum."""
    floor = version_tuple(minimum_version)
    version = active_claude_version()
    if version_tuple(version) < floor:
        raise ClaudeRuntimeError('active Claude Code runtime is below minimum version')
    binary = active_claude_binary(env=env)
    actual = probe_claude_version(binary, env=env, cwd=cwd, timeout=timeout)
    if actual != version:
        _version_cache.pop(str(binary), None)
        raise ClaudeRuntimeError('active Claude Code runtime version mismatch')
    return actual

def claude_runtime_identity(*, env: Optional[dict[str, str]] = None) -> str:
    version = require_managed_claude_runtime(env=env)
    return 'claude-code:%s' % version


def claude_argv_prefix(*, env: Optional[dict[str, str]] = None) -> list[str]:
    """Return only the exact native binary selected by active-version."""
    return [str(active_claude_binary(env=env))]

def claude_cmd_for_version(
    version: str,
    *args: str,
    env: Optional[dict[str, str]] = None,
) -> list[str]:
    """Construct argv for one validated immutable native version path."""
    binary = native_claude_binary(version, env=env)
    return [str(binary), *[str(arg) for arg in args]]


def claude_cmd(*args: str, root: Optional[Path] = None, env: Optional[dict[str, str]] = None) -> list[str]:
    del root  # retained as a compatibility keyword; runtime is not repository-local.
    return claude_cmd_for_version(active_claude_version(), *args, env=env)

def require_pinned_claude_version(**kwargs) -> str:
    """Deprecated compatibility alias; enforces the managed minimum contract."""
    return require_managed_claude_runtime(**kwargs)


def pinned_runtime_available(*, env=None, cwd=None, root=None) -> bool:
    del root
    try:
        require_managed_claude_runtime(env=env, cwd=cwd, timeout=15.0)
        return True
    except Exception:
        return False


def clear_version_cache() -> None:
    _version_cache.clear()


def runtime_status_dict(*, env=None, cwd=None, root=None) -> dict[str, Any]:
    del root
    active = None
    binary_exists = False
    error_code = None
    try:
        active = active_claude_version()
        binary = active_claude_binary(env=env)
        binary_exists = True
        actual = probe_claude_version(binary, env=env, cwd=cwd, timeout=10.0)
        if actual != active:
            raise ClaudeRuntimeError('active Claude Code runtime version mismatch')
        if version_tuple(active) < version_tuple(MINIMUM_CLAUDE_CODE_VERSION):
            error_code = 'runtime_below_minimum'
        else:
            error_code = None
    except ClaudeRuntimeError as exc:
        code = str(exc)
        if 'below minimum' in code:
            error_code = 'runtime_below_minimum'
        elif 'missing' in code or 'unavailable' in code:
            error_code = 'runtime_unavailable'
        elif 'mismatch' in code:
            error_code = 'runtime_version_mismatch'
        else:
            error_code = 'runtime_invalid'
    return {
        'minimum_version': MINIMUM_CLAUDE_CODE_VERSION,
        'active_version': active,
        'binary_exists': binary_exists,
        'identity': ('claude-code:%s' % active) if active and binary_exists else None,
        'status': 'healthy' if error_code is None and active else ('uninitialized' if active is None else 'error'),
        'error_code': error_code,
    }
