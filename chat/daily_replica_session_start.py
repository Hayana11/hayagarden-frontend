"""SessionStart freeze + isolation for 9A-R replica residents.

Production Claude Code merges user/project settings and runs dynamic SessionStart
hooks on every startup/resume.  Replicas must see one frozen payload for both A
(startup) and B (resume) without re-running production hooks.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from chat.daily_replica_ab import ReplicaContractError

PRODUCTION_HOOK_MARKERS: tuple[str, ...] = (
    'session_breath.py',
    'session_context_inject.py',
)

DEFAULT_BREATH_HOOK_PATH = Path('/opt/ombre-brain/.claude/hooks/session_breath.py')
DEFAULT_CONTEXT_FILE_PATH = Path('/tmp/session_context.txt')

CONTEXT_INJECT_WRAPPER_PREFIX = '[上一个窗口的对话记忆]\n'
OMBRE_BREATH_WRAPPER_PREFIX = '[Ombre Brain - 记忆浮现]\n'

_STATIC_HOOK_SOURCE = '''#!/usr/bin/env python3
import sys
from pathlib import Path
path = Path(__file__).resolve().parent.parent / "frozen_session_start.txt"
sys.stdout.write(path.read_text(encoding="utf-8"))
'''


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_text(value: str) -> str:
    return _sha256_bytes(str(value or '').encode('utf-8'))


def resolve_replica_settings_path(claude_config_dir: Path | str) -> Path:
    """Replica settings live directly under ``CLAUDE_CONFIG_DIR``."""
    return Path(claude_config_dir) / 'settings.json'


def _freeze_context_inject_visible_readonly(
    *,
    context_file_path: Path = DEFAULT_CONTEXT_FILE_PATH,
) -> str:
    """Model-visible output of session_context_inject without executing or deleting."""
    path = Path(context_file_path)
    if not path.is_file():
        return ''
    try:
        content = path.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return ''
    if not content:
        return ''
    return CONTEXT_INJECT_WRAPPER_PREFIX + content


def _capture_session_breath_visible(
    *,
    breath_hook_path: Optional[Path] = None,
    breath_runner: Optional[Callable[[], str]] = None,
) -> str:
    """Model-visible stdout from session_breath (Ombre Brain), including wrapper."""
    if breath_runner is not None:
        return str(breath_runner() or '').strip()
    hook = Path(breath_hook_path or DEFAULT_BREATH_HOOK_PATH)
    if not hook.is_file():
        return ''
    try:
        proc = subprocess.run(
            ['python3', str(hook)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    if proc.returncode != 0:
        return ''
    return str(proc.stdout or '').strip()


def capture_readonly_production_session_start_visible(
    *,
    context_file_path: Path = DEFAULT_CONTEXT_FILE_PATH,
    breath_hook_path: Optional[Path] = None,
    breath_runner: Optional[Callable[[], str]] = None,
) -> str:
    """Freeze full model-visible SessionStart output from both production hooks.

    Never executes session_context_inject.py (which would delete the context file).
    """
    parts: list[str] = []
    breath = _capture_session_breath_visible(
        breath_hook_path=breath_hook_path,
        breath_runner=breath_runner,
    )
    if breath:
        parts.append(breath)
    context_visible = _freeze_context_inject_visible_readonly(
        context_file_path=context_file_path,
    )
    if context_visible:
        parts.append(context_visible)
    return '\n'.join(parts)


@dataclass(frozen=True)
class ReplicaSessionStartBundle:
    frozen_path: Path
    frozen_sha256: str
    replica_settings_sha256: str
    claude_home: Path
    fake_home: Path
    replica_cwd: Path
    static_hook_path: Path
    settings_path: Path
    session_start_isolation_ok: bool
    production_hooks_blocked: tuple[str, ...]

    def isolation_env(self) -> dict[str, str]:
        return {
            'HOME': str(self.fake_home),
            'CLAUDE_CONFIG_DIR': str(self.claude_home),
            'DISABLE_AUTOUPDATER': '1',
        }

    def merge_env(self, base: Mapping[str, str]) -> dict[str, str]:
        merged = dict(base)
        merged.update(self.isolation_env())
        return merged


def _settings_references_production_hooks(settings_text: str) -> list[str]:
    lowered = settings_text.lower()
    hits: list[str] = []
    for marker in PRODUCTION_HOOK_MARKERS:
        if marker.lower() in lowered:
            hits.append(marker)
    return hits


def read_static_session_start_payload(bundle: ReplicaSessionStartBundle) -> str:
    """What the replica static SessionStart hook would emit (no Claude spawn)."""
    return bundle.frozen_path.read_text(encoding='utf-8')


def validate_session_start_bundle(bundle: ReplicaSessionStartBundle) -> None:
    """Fail closed before replica resident spawn."""
    if not bundle.session_start_isolation_ok:
        raise ReplicaContractError(
            'session start isolation proof is incomplete',
            error_code='REPLICA_SESSION_START_ISOLATION_FAILED',
        )
    if not bundle.frozen_path.is_file():
        raise ReplicaContractError(
            'frozen SessionStart payload missing',
            error_code='REPLICA_SESSION_START_FROZEN_MISSING',
        )
    on_disk = _sha256_file(bundle.frozen_path)
    if on_disk != bundle.frozen_sha256:
        raise ReplicaContractError(
            'frozen SessionStart payload hash changed',
            error_code='REPLICA_SESSION_START_HASH_MISMATCH',
        )
    if not bundle.settings_path.is_file():
        raise ReplicaContractError(
            'replica settings missing',
            error_code='REPLICA_SESSION_START_ISOLATION_FAILED',
        )
    expected_settings = resolve_replica_settings_path(bundle.claude_home)
    if bundle.settings_path.resolve() != expected_settings.resolve():
        raise ReplicaContractError(
            'replica settings path does not match CLAUDE_CONFIG_DIR',
            error_code='REPLICA_SESSION_START_ISOLATION_FAILED',
        )
    settings_text = bundle.settings_path.read_text(encoding='utf-8')
    if _sha256_text(settings_text) != bundle.replica_settings_sha256:
        raise ReplicaContractError(
            'replica settings hash changed',
            error_code='REPLICA_SESSION_START_SETTINGS_MISMATCH',
        )
    if _settings_references_production_hooks(settings_text):
        raise ReplicaContractError(
            'replica settings reference production SessionStart hooks',
            error_code='REPLICA_SESSION_START_ISOLATION_FAILED',
        )


def validate_session_start_against_a_hash(
    bundle: ReplicaSessionStartBundle,
    a_frozen_session_start_sha256: Optional[str],
) -> None:
    """B must receive the exact frozen hash observed for A before any seed/resident."""
    validate_session_start_bundle(bundle)
    expected = str(a_frozen_session_start_sha256 or '').strip()
    if not expected:
        raise ReplicaContractError(
            'A frozen SessionStart hash is required for B',
            error_code='REPLICA_SESSION_START_A_HASH_MISSING',
        )
    if bundle.frozen_sha256 != expected:
        raise ReplicaContractError(
            'B SessionStart frozen hash does not match A',
            error_code='REPLICA_SESSION_START_HASH_MISMATCH',
        )


def prepare_replica_session_start_isolation(
    temp_root: Path,
    *,
    material_capturer: Optional[Callable[[], str]] = None,
    production_hook_markers: Sequence[str] = PRODUCTION_HOOK_MARKERS,
) -> ReplicaSessionStartBundle:
    """Freeze SessionStart-visible material once under ``temp_root``."""
    root = Path(temp_root)
    session_dir = root / 'session-start'
    replica_cwd = root / 'replica-cwd'
    claude_home = root / 'claude-home'
    fake_home = root / 'fake-home'
    hooks_dir = session_dir / 'hooks'
    session_dir.mkdir(parents=True, exist_ok=True)
    replica_cwd.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    fake_home.mkdir(parents=True, exist_ok=True)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    capturer = material_capturer or capture_readonly_production_session_start_visible
    frozen_text = str(capturer() or '')
    frozen_path = session_dir / 'frozen_session_start.txt'
    frozen_path.write_text(frozen_text, encoding='utf-8')

    static_hook_path = hooks_dir / 'static_session_start.py'
    static_hook_path.write_text(_STATIC_HOOK_SOURCE, encoding='utf-8')
    os.chmod(static_hook_path, 0o700)

    settings = {
        'hooks': {
            'SessionStart': [
                {
                    'matcher': '',
                    'hooks': [
                        {
                            'type': 'command',
                            'command': f'python3 {static_hook_path}',
                        },
                    ],
                },
            ],
        },
    }
    settings_text = json.dumps(settings, ensure_ascii=False, indent=2)
    settings_hits = _settings_references_production_hooks(settings_text)
    if settings_hits:
        raise ReplicaContractError(
            'replica settings still reference production SessionStart hooks',
            error_code='REPLICA_SESSION_START_ISOLATION_FAILED',
        )

    settings_path = resolve_replica_settings_path(claude_home)
    settings_path.write_text(settings_text, encoding='utf-8')

    # Empty user-home Claude config; OAuth token comes from caller env.
    (fake_home / '.claude.json').write_text('{}', encoding='utf-8')

    frozen_sha = _sha256_file(frozen_path)
    settings_sha = _sha256_text(settings_text)
    isolation_ok = bool(
        frozen_path.is_file()
        and settings_path.is_file()
        and not settings_hits
        and settings_path.resolve() == resolve_replica_settings_path(claude_home).resolve()
    )

    return ReplicaSessionStartBundle(
        frozen_path=frozen_path,
        frozen_sha256=frozen_sha,
        replica_settings_sha256=settings_sha,
        claude_home=claude_home,
        fake_home=fake_home,
        replica_cwd=replica_cwd,
        static_hook_path=static_hook_path,
        settings_path=settings_path,
        session_start_isolation_ok=isolation_ok,
        production_hooks_blocked=tuple(production_hook_markers),
    )
