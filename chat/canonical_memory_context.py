"""Read-only bridge from the canonical GitHub memory archive into Chat context.

This module is intentionally boring.  It does not clone/pull Git, mutate the
archive, promote candidates, read Source originals, or decide what long-term
records are relevant.  It only gives the runtime a safe local-filesystem view
of the G1/G2 canonical archive contract so later wiring can inject the small
``recent/current.md`` capsule once at session cold-start and discover indexed
records on demand.

The bridge is inert unless ``CANONICAL_MEMORY_CONTEXT_ENABLED`` is true.  The
archive root is supplied by ``HAYAGARDEN_CANONICAL_MEMORY_ROOT``; there is no
production-path default because the live VPS currently has an older Ombre
checkout at ``/opt/ombre-brain`` and must never be mistaken for the canonical
G2 archive.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

import config_store

_ENABLE_KEY = 'CANONICAL_MEMORY_CONTEXT_ENABLED'
_ROOT_ENV = 'HAYAGARDEN_CANONICAL_MEMORY_ROOT'

_MAX_PERSONA_BYTES = 256 * 1024
_MAX_CURRENT_BYTES = 32 * 1024
_MAX_INDEX_BYTES = 512 * 1024

_EMPTY_PERSONA_STATUSES = {'EMPTY_G2_SLOT', 'EMPTY'}
_EMPTY_CURRENT_STATUSES = {'EMPTY', 'EMPTY_G2_SLOT'}


@dataclass(frozen=True)
class MemoryIndexEntry:
    record_id: str
    record_type: str
    title: str
    status: str
    time: str
    sensitivity: str
    path: str


@dataclass(frozen=True)
class CanonicalMemoryContext:
    enabled: bool
    status: str
    root: Optional[str]
    persona_text: str
    current_text: str
    current_sha256: Optional[str]
    index_entries: Tuple[MemoryIndexEntry, ...]
    diagnostics: Tuple[str, ...]

    @property
    def has_current(self) -> bool:
        return bool(self.current_text.strip())

    @property
    def has_discoverable_memory(self) -> bool:
        return bool(self.index_entries)


class CanonicalMemoryPathError(RuntimeError):
    """Raised internally when an archive path violates the read boundary."""


def _flag_enabled(explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return bool(explicit)
    try:
        return bool(config_store.get_bool(_ENABLE_KEY, False))
    except Exception:
        return False


def _configured_root(explicit: Optional[os.PathLike | str]) -> Optional[Path]:
    if explicit is not None:
        raw = str(explicit).strip()
    else:
        raw = os.environ.get(_ROOT_ENV, '').strip()
    return Path(raw) if raw else None


def _resolve_root(root: Path) -> Path:
    if root.is_symlink():
        raise CanonicalMemoryPathError('archive_root_symlink')
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CanonicalMemoryPathError('archive_root_missing') from exc
    if not resolved.is_dir():
        raise CanonicalMemoryPathError('archive_root_not_directory')
    return resolved


def _safe_child(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise CanonicalMemoryPathError(f'unsafe_relative_path:{relative}')
    candidate = root / rel
    if candidate.is_symlink():
        raise CanonicalMemoryPathError(f'file_symlink:{relative}')
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise CanonicalMemoryPathError(f'file_missing:{relative}') from exc
    except (OSError, RuntimeError) as exc:
        raise CanonicalMemoryPathError(f'file_unreadable:{relative}') from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CanonicalMemoryPathError(f'path_escape:{relative}') from exc
    if not resolved.is_file():
        raise CanonicalMemoryPathError(f'not_regular_file:{relative}')
    return resolved


def _read_utf8(root: Path, relative: str, max_bytes: int) -> str:
    path = _safe_child(root, relative)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CanonicalMemoryPathError(f'file_stat_failed:{relative}') from exc
    if size > max_bytes:
        raise CanonicalMemoryPathError(f'file_too_large:{relative}:{size}')
    try:
        return path.read_text(encoding='utf-8').strip()
    except UnicodeDecodeError as exc:
        raise CanonicalMemoryPathError(f'file_not_utf8:{relative}') from exc
    except OSError as exc:
        raise CanonicalMemoryPathError(f'file_read_failed:{relative}') from exc


def _status(text: str) -> str:
    for line in str(text).splitlines()[:12]:
        stripped = line.strip()
        if stripped.lower().startswith('status:'):
            value = stripped.split(':', 1)[1].strip().strip('`').strip()
            return value
    return ''


def _non_placeholder(text: str, empty_statuses: Iterable[str]) -> str:
    compact = str(text or '').strip()
    if not compact:
        return ''
    if _status(compact) in set(empty_statuses):
        return ''
    return compact


def _safe_index_path(value: str) -> bool:
    raw = str(value or '').strip().strip('`')
    if not raw:
        return False
    path = Path(raw)
    return not path.is_absolute() and '..' not in path.parts


def _parse_index(text: str) -> Tuple[MemoryIndexEntry, ...]:
    """Parse only the lightweight seven-column directory card.

    The index is discovery metadata.  This function never follows a record
    path and therefore cannot accidentally turn discovery into default recall.
    """
    rows = []
    for line in str(text or '').splitlines():
        stripped = line.strip()
        if not (stripped.startswith('|') and stripped.endswith('|')):
            continue
        cells = [cell.strip().strip('`') for cell in stripped[1:-1].split('|')]
        if len(cells) != 7:
            continue
        if cells[0].lower() in {'id', 'record id'}:
            continue
        if all(set(cell) <= {'-', ':'} for cell in cells if cell):
            continue
        if not cells[0] or not _safe_index_path(cells[6]):
            continue
        rows.append(MemoryIndexEntry(*cells))
    return tuple(rows)


def load_canonical_memory_context(
    *,
    enabled: Optional[bool] = None,
    root: Optional[os.PathLike | str] = None,
) -> CanonicalMemoryContext:
    """Return a read-only snapshot; archive problems degrade to no injection.

    A broken/missing canonical archive must never make ordinary chat unavailable.
    The caller gets diagnostics and the legacy/runtime authorities remain in
    charge.  No guessed fallback root is attempted.
    """
    if not _flag_enabled(enabled):
        return CanonicalMemoryContext(
            enabled=False,
            status='disabled',
            root=None,
            persona_text='',
            current_text='',
            current_sha256=None,
            index_entries=(),
            diagnostics=(),
        )

    configured = _configured_root(root)
    if configured is None:
        return CanonicalMemoryContext(
            enabled=True,
            status='unavailable',
            root=None,
            persona_text='',
            current_text='',
            current_sha256=None,
            index_entries=(),
            diagnostics=('archive_root_unconfigured',),
        )

    diagnostics = []
    try:
        resolved_root = _resolve_root(configured)
    except CanonicalMemoryPathError as exc:
        return CanonicalMemoryContext(
            enabled=True,
            status='unavailable',
            root=str(configured),
            persona_text='',
            current_text='',
            current_sha256=None,
            index_entries=(),
            diagnostics=(str(exc),),
        )

    persona = ''
    current = ''
    index_entries: Tuple[MemoryIndexEntry, ...] = ()

    try:
        persona = _non_placeholder(
            _read_utf8(resolved_root, 'identity/persona.md', _MAX_PERSONA_BYTES),
            _EMPTY_PERSONA_STATUSES,
        )
    except CanonicalMemoryPathError as exc:
        diagnostics.append(str(exc))

    try:
        current = _non_placeholder(
            _read_utf8(resolved_root, 'recent/current.md', _MAX_CURRENT_BYTES),
            _EMPTY_CURRENT_STATUSES,
        )
    except CanonicalMemoryPathError as exc:
        diagnostics.append(str(exc))

    try:
        index_entries = _parse_index(
            _read_utf8(resolved_root, 'MEMORY_INDEX.md', _MAX_INDEX_BYTES),
        )
    except CanonicalMemoryPathError as exc:
        diagnostics.append(str(exc))

    current_sha = (
        hashlib.sha256(current.encode('utf-8')).hexdigest()
        if current else None
    )
    has_payload = bool(persona or current or index_entries)
    return CanonicalMemoryContext(
        enabled=True,
        status='ready' if has_payload else 'empty',
        root=str(resolved_root),
        persona_text=persona,
        current_text=current,
        current_sha256=current_sha,
        index_entries=index_entries,
        diagnostics=tuple(diagnostics),
    )


def format_current_for_cold_once(snapshot: CanonicalMemoryContext) -> str:
    """Format the only G1 memory body allowed to default-inject.

    Persona is deliberately *not* returned here: production persona stays under
    ``chat.persona_store`` until a separate explicit authority cutover.  Index
    entries are discovery-only and long-term bodies remain on-demand.
    """
    if not snapshot.enabled or not snapshot.has_current:
        return ''
    return '## 长期记忆档案 · 当前近况\n' + snapshot.current_text.strip()
