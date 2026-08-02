"""SessionStart freeze + isolation for 9A-R replica residents.

Production Claude Code merges user/project settings and runs dynamic SessionStart
hooks on every startup/resume.  Replicas must see one frozen payload for both A
(startup) and B (resume) without re-running production hooks.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from chat.daily_replica_ab import ReplicaContractError

PRODUCTION_HOOK_MARKERS: tuple[str, ...] = (
    'session_breath.py',
    'session_context_inject.py',
)

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


def capture_readonly_session_start_sources() -> str:
    """Read-only snapshot of known production SessionStart inputs; never deletes."""
    parts: list[str] = []
    ctx_path = Path('/tmp/session_context.txt')
    if ctx_path.is_file():
        try:
            text = ctx_path.read_text(encoding='utf-8', errors='replace')
            if text:
                parts.append(text)
        except OSError:
            pass
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

    capturer = material_capturer or capture_readonly_session_start_sources
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

    claude_settings_dir = claude_home / '.claude'
    claude_settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_settings_dir / 'settings.json'
    settings_path.write_text(settings_text, encoding='utf-8')

    # Empty user-home Claude config; OAuth token comes from caller env.
    (fake_home / '.claude.json').write_text('{}', encoding='utf-8')

    frozen_sha = _sha256_file(frozen_path)
    settings_sha = _sha256_text(settings_text)
    isolation_ok = bool(frozen_path.is_file() and settings_path.is_file() and not settings_hits)

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
