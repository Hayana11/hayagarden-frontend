"""Nexus workspace path guard — thin fail-closed resolver.

Production root: /opt/workspace/projects/nexus/current
Importing this module never creates the production directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DEFAULT_NEXUS_WORKSPACE_ROOT = "/opt/workspace/projects/nexus/current"
_FORBIDDEN_FALLBACKS = (
    "/opt/frontend",
)


class NexusPathError(ValueError):
    """Fail-closed path or workspace root error."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or code


def configured_nexus_root(*, override: Optional[str] = None) -> str:
    """Return the configured root string without creating directories."""
    if override is not None:
        raw = str(override).strip()
    else:
        raw = str(os.environ.get("NEXUS_WORKSPACE_ROOT", DEFAULT_NEXUS_WORKSPACE_ROOT)).strip()
    if not raw:
        raise NexusPathError("workspace_root_unconfigured")
    return raw


def resolve_nexus_workspace(*, override: Optional[str] = None) -> Path:
    """Resolve and validate the Nexus workspace root.

    - Does not create directories.
    - Requires an absolute path.
    - Requires the path to exist and be a directory.
    - Rejects symlink escapes relative to the unresolved parent chain
      when the configured path itself escapes a forbidden fallback.
    - Never falls back to /opt/frontend, cwd, or HOME.
    """
    raw = configured_nexus_root(override=override)
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise NexusPathError("workspace_root_must_be_absolute", raw)

    # Reject explicit forbidden fallbacks before resolve.
    normalized = os.path.normpath(raw)
    for forbidden in _FORBIDDEN_FALLBACKS:
        if normalized == forbidden or normalized.startswith(forbidden + os.sep):
            raise NexusPathError("workspace_root_forbidden", raw)

    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise NexusPathError("workspace_root_unresolvable", raw) from exc

    resolved_s = str(resolved)
    for forbidden in _FORBIDDEN_FALLBACKS:
        if resolved_s == forbidden or resolved_s.startswith(forbidden + os.sep):
            raise NexusPathError("workspace_root_forbidden", resolved_s)

    if not resolved.exists():
        raise NexusPathError("workspace_root_missing", resolved_s)
    if not resolved.is_dir():
        raise NexusPathError("workspace_root_not_directory", resolved_s)
    if not resolved.is_absolute():
        raise NexusPathError("workspace_root_must_be_absolute", resolved_s)

    return resolved


def resolve_under_nexus(root: Path, relative: str) -> Path:
    """Resolve a relative path under an already-validated Nexus root.

    Rejects empty paths, absolute inputs, `..` traversal, and symlink escapes.
    """
    raw = str(relative or "").strip()
    if not raw:
        raise NexusPathError("path_required")
    if raw.startswith("/") or raw.startswith("~"):
        raise NexusPathError("path_absolute_rejected", raw)
    # Explicit .. segment rejection before resolve.
    parts = Path(raw).parts
    if any(p == ".." for p in parts):
        raise NexusPathError("path_traversal_rejected", raw)

    try:
        root_resolved = root.resolve(strict=True)
    except (OSError, FileNotFoundError) as exc:
        raise NexusPathError("workspace_root_invalid", str(root)) from exc

    target = (root_resolved / raw).resolve(strict=False)
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise NexusPathError("path_outside_nexus", str(target)) from exc
    return target


def assert_path_inside(root: Path, path: Path) -> Path:
    """Fail-closed check that path stays inside root after realpath."""
    try:
        root_resolved = root.resolve(strict=True)
        path_resolved = path.resolve(strict=False)
        path_resolved.relative_to(root_resolved)
    except (OSError, ValueError, FileNotFoundError) as exc:
        raise NexusPathError("path_outside_nexus", str(path)) from exc
    return path_resolved
