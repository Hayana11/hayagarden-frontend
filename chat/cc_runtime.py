"""Pinned Claude Code runtime contract for production Fyodor / Manual Forge.

Production must not invoke bare PATH ``claude`` (global package drift).
All resident spawns resolve through this module and fail closed when the
actual CLI version is not ``EXPECTED_CLAUDE_CODE_VERSION``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, Sequence

EXPECTED_CLAUDE_CODE_VERSION = '2.1.220'
CLAUDE_CODE_NPM_PACKAGE = '@anthropic-ai/claude-code'
CLAUDE_CODE_NPM_SPEC = '%s@%s' % (CLAUDE_CODE_NPM_PACKAGE, EXPECTED_CLAUDE_CODE_VERSION)
RUNTIME_DIR_NAME = '.claude-runtime'

# Optional test/ops override: JSON array argv prefix, e.g. '["/tmp/fake-claude"]'.
ARGV_OVERRIDE_ENV = 'HAYA_CLAUDE_ARGV_JSON'
# Optional: skip live --version probe (unit tests only). Still requires override argv.
SKIP_VERSION_PROBE_ENV = 'HAYA_CLAUDE_SKIP_VERSION_PROBE'

_VERSION_RE = re.compile(r'(\d+\.\d+\.\d+)')
_cache_lock_version: Optional[tuple[str, str, float]] = None  # (argv_key, version, monotonic)


class ClaudeRuntimeError(RuntimeError):
    """Pinned Claude Code runtime missing or version mismatch."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def runtime_prefix_dir(root: Optional[Path] = None) -> Path:
    return (root or repo_root()) / RUNTIME_DIR_NAME


def local_claude_bin(root: Optional[Path] = None) -> Optional[Path]:
    """Return project-local pinned binary if present."""
    base = (
        runtime_prefix_dir(root)
        / 'node_modules'
        / '@anthropic-ai'
        / 'claude-code'
        / 'bin'
    )
    for name in ('claude', 'claude.exe'):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def claude_argv_prefix(root: Optional[Path] = None) -> list[str]:
    """Argv prefix that starts the pinned Claude Code consumer.

    Prefer ``.claude-runtime`` install (deploy-managed). Fall back to
    ``npx --yes @anthropic-ai/claude-code@EXPECTED`` so the pin is still
    explicit when the local tree is not yet installed.
    Never returns bare ``['claude']``.
    """
    raw = (os.environ.get(ARGV_OVERRIDE_ENV) or '').strip()
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed or not all(isinstance(x, str) for x in parsed):
            raise ClaudeRuntimeError('%s must be a JSON string array' % ARGV_OVERRIDE_ENV)
        return list(parsed)

    local = local_claude_bin(root)
    if local is not None:
        return [str(local)]
    return ['npx', '--yes', CLAUDE_CODE_NPM_SPEC]


def claude_cmd(*args: str, root: Optional[Path] = None) -> list[str]:
    return claude_argv_prefix(root=root) + [str(a) for a in args]


def parse_claude_version_text(text: str) -> str:
    match = _VERSION_RE.search(text or '')
    if not match:
        raise ClaudeRuntimeError('unparseable claude --version output: %r' % (text or '')[:200])
    return match.group(1)


def probe_claude_version(
    *,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    root: Optional[Path] = None,
    timeout: float = 60.0,
) -> str:
    prefix = claude_argv_prefix(root=root)
    proc = subprocess.run(
        prefix + ['--version'],
        cwd=cwd or str(repo_root()),
        env=env or os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    blob = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
    if proc.returncode != 0:
        raise ClaudeRuntimeError(
            'claude --version failed (exit %s): %s' % (proc.returncode, blob[:300])
        )
    return parse_claude_version_text(blob)


def require_pinned_claude_version(
    *,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    root: Optional[Path] = None,
    timeout: float = 60.0,
) -> str:
    """Return actual version; raise ClaudeRuntimeError on mismatch/unavailable."""
    global _cache_lock_version
    prefix = claude_argv_prefix(root=root)
    argv_key = json.dumps(prefix)

    if (os.environ.get(SKIP_VERSION_PROBE_ENV) or '').strip() in ('1', 'true', 'yes'):
        # Tests supply a fake argv; treat expected pin as satisfied.
        return EXPECTED_CLAUDE_CODE_VERSION

    now = time.monotonic()
    cached = _cache_lock_version
    if cached and cached[0] == argv_key and (now - cached[2]) < 300.0:
        actual = cached[1]
    else:
        actual = probe_claude_version(env=env, cwd=cwd, root=root, timeout=timeout)
        _cache_lock_version = (argv_key, actual, now)

    if actual != EXPECTED_CLAUDE_CODE_VERSION:
        raise ClaudeRuntimeError(
            'claude runtime version mismatch: expected %s, got %s (argv=%s)'
            % (EXPECTED_CLAUDE_CODE_VERSION, actual, prefix)
        )
    return actual


def pinned_runtime_available(
    *,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    root: Optional[Path] = None,
) -> bool:
    try:
        require_pinned_claude_version(env=env, cwd=cwd, root=root, timeout=30.0)
        return True
    except Exception:
        return False


def clear_version_cache() -> None:
    global _cache_lock_version
    _cache_lock_version = None


def runtime_status_dict(
    *,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    root: Optional[Path] = None,
) -> dict[str, Any]:
    prefix = claude_argv_prefix(root=root)
    out: dict[str, Any] = {
        'expected_version': EXPECTED_CLAUDE_CODE_VERSION,
        'npm_spec': CLAUDE_CODE_NPM_SPEC,
        'argv_prefix': prefix,
        'local_bin': str(local_claude_bin(root) or ''),
        'ok': False,
        'actual_version': '',
        'error': '',
    }
    try:
        out['actual_version'] = require_pinned_claude_version(env=env, cwd=cwd, root=root)
        out['ok'] = True
    except Exception as exc:
        out['error'] = str(exc)
    return out
