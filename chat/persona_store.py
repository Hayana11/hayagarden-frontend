"""Production persona runtime authority.

``/var/lib/hayagarden/persona.md`` is the only runtime authority for the
live product. ``/opt/frontend/prompts/persona.md`` is a repository seed /
disaster-recovery fallback used solely when the runtime file has never been
established.

Once the runtime authority exists, Git checkout, PR deploy, and repo seed
updates must never overwrite it.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

PathLike = Union[str, os.PathLike]

RUNTIME_PERSONA_PATH = '/var/lib/hayagarden/persona.md'
REPO_PERSONA_FALLBACK_PATH = '/opt/frontend/prompts/persona.md'


class PersonaStoreError(RuntimeError):
    """Fail-closed persona authority error."""


def _runtime_path() -> Path:
    return Path(RUNTIME_PERSONA_PATH)


def _fallback_path() -> Path:
    return Path(REPO_PERSONA_FALLBACK_PATH)


def _read_text(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _is_usable_persona(text: str) -> bool:
    return bool(str(text).strip())


def ensure_runtime_persona() -> Path:
    """Bootstrap runtime authority from repo seed when absent.

    If the runtime file already exists, leave it untouched even when the repo
    seed has changed.
    """
    runtime = _runtime_path()
    if runtime.exists():
        return runtime

    fallback = _fallback_path()
    try:
        seed = _read_text(fallback)
    except FileNotFoundError as exc:
        raise PersonaStoreError(
            f'persona runtime missing and repo fallback absent: {fallback}',
        ) from exc
    except OSError as exc:
        raise PersonaStoreError(
            f'persona runtime missing and repo fallback unreadable: {fallback}',
        ) from exc

    if not _is_usable_persona(seed):
        raise PersonaStoreError(
            f'persona runtime missing and repo fallback empty: {fallback}',
        )

    write_persona(seed)
    return runtime


def read_persona() -> str:
    """Read production persona from runtime authority.

    Bootstraps from the repo seed only when the runtime file has never been
    created. An existing but empty/unreadable runtime file is fail-closed and
    never silently replaced by the repo seed.
    """
    runtime = _runtime_path()
    if not runtime.exists():
        ensure_runtime_persona()

    try:
        text = _read_text(runtime)
    except OSError as exc:
        raise PersonaStoreError(
            f'persona runtime unreadable: {runtime}',
        ) from exc

    if not _is_usable_persona(text):
        raise PersonaStoreError(
            f'persona runtime empty or invalid: {runtime}',
        )
    return text


def write_persona(content: str) -> None:
    """Atomically replace the runtime persona authority."""
    text = str(content)
    if not _is_usable_persona(text):
        raise PersonaStoreError('persona content must be non-empty')

    runtime = _runtime_path()
    runtime.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix='.persona.',
        suffix='.tmp',
        dir=str(runtime.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(runtime))
        try:
            runtime.chmod(0o644)
        except OSError:
            pass
        try:
            dir_fd = os.open(str(runtime.parent), os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except TypeError:
            # Python <3.8 compatibility not required; keep belt-and-suspenders.
            if tmp_path.exists():
                tmp_path.unlink()
        raise
