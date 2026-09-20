"""Read-only git summary for the fixed Nexus workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from nexus_paths import NexusPathError, assert_path_inside

_GIT_TIMEOUT = 15

# Untracked directories: we expand to concrete files via `git ls-files --others`.
# Directory entries themselves are not counted as additions.
UNTRACKED_DIR_POLICY = "expand_files"


def _run_git(
    workspace: Path, args: list[str], *, binary: bool = False
) -> subprocess.CompletedProcess:
    assert_path_inside(workspace, workspace)
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=not binary,
            errors="replace" if not binary else None,
            timeout=_GIT_TIMEOUT,
            check=False,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NexusPathError("git_unavailable", str(exc)) from exc


def _stdout(proc: subprocess.CompletedProcess) -> str:
    out = proc.stdout or ("" if not isinstance(proc.stdout, (bytes, bytearray)) else b"")
    if isinstance(out, (bytes, bytearray)):
        return out.decode("utf-8", errors="surrogateescape")
    return out


def _ensure_git_worktree(workspace: Path) -> None:
    proc = _run_git(workspace, ["rev-parse", "--is-inside-work-tree"])
    if proc.returncode != 0 or _stdout(proc).strip().lower() != "true":
        detail = (_stdout(proc) or (proc.stderr or b"" if isinstance(proc.stderr, bytes) else proc.stderr) or "")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise NexusPathError("not_a_git_worktree", str(detail).strip()[:200] or "not a git worktree")


def _parse_numstat(text: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            if parts[0] != "-":
                additions += int(parts[0])
            if parts[1] != "-":
                deletions += int(parts[1])
        except ValueError:
            continue
    return additions, deletions


def _parse_z_paths(data: bytes) -> list[str]:
    """Parse NUL-separated path lists from git -z outputs (no quoting/escaping)."""
    if not data:
        return []
    parts = data.split(b"\0")
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        out.append(part.decode("utf-8", errors="surrogateescape"))
    return out


def _parse_porcelain_z(data: bytes) -> list[tuple[str, str]]:
    """Parse `git status --porcelain -z` into (status, path) pairs.

    For rename/copy, porcelain ``-z`` emits destination first, then source.
    ``changed_files`` keeps only the destination (target) path and skips source.
    Paths are returned without Git shell quoting.
    """
    if not data:
        return []
    entries: list[tuple[str, str]] = []
    parts = data.split(b"\0")
    i = 0
    while i < len(parts):
        chunk = parts[i]
        if not chunk:
            i += 1
            continue
        if len(chunk) < 3:
            i += 1
            continue
        status = chunk[:2].decode("ascii", errors="replace")
        # porcelain -z: first record is "XY path" (space after status)
        name = chunk[3:] if chunk[2:3] == b" " else chunk[2:]
        # First path is the destination/target for rename/copy.
        path = name.decode("utf-8", errors="surrogateescape")
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            # Skip the second NUL field (original/source path).
            if i + 1 < len(parts) and parts[i + 1]:
                i += 2
            else:
                i += 1
        else:
            i += 1
        if path:
            entries.append((status, path))
    return entries


def _untracked_line_count(workspace: Path, rel: str) -> int:
    path = workspace / rel
    try:
        if not path.is_file():
            return 0
        data = path.read_bytes()[:512_000]
        if not data:
            return 0
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
    except OSError:
        return 0


def git_summary(workspace: Path) -> dict[str, Any]:
    """Return the frozen six-field git summary.

    Covers staged + unstaged (via diff against HEAD) and untracked files
    (expanded via ``git ls-files --others``; directories are not counted as
    additions — see UNTRACKED_DIR_POLICY).
    """
    _ensure_git_worktree(workspace)

    branch_proc = _run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = _stdout(branch_proc).strip() or "HEAD"

    porcelain_proc = _run_git(workspace, ["status", "--porcelain", "-z"], binary=True)
    porcelain_entries = _parse_porcelain_z(porcelain_proc.stdout or b"")

    diff_stat = _stdout(_run_git(workspace, ["diff", "HEAD", "--stat"])).rstrip()
    numstat = _stdout(_run_git(workspace, ["diff", "HEAD", "--numstat"]))
    name_only_proc = _run_git(workspace, ["diff", "HEAD", "-z", "--name-only"], binary=True)
    changed = _parse_z_paths(name_only_proc.stdout or b"")

    additions, deletions = _parse_numstat(numstat)

    # Expand untracked to concrete files (never treat a directory as one "addition").
    others_proc = _run_git(
        workspace,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        binary=True,
    )
    untracked_files = _parse_z_paths(others_proc.stdout or b"")

    for status, path in porcelain_entries:
        if path not in changed:
            changed.append(path)

    for path in untracked_files:
        if path not in changed:
            changed.append(path)

    untracked_additions = 0
    for path in untracked_files:
        untracked_additions += _untracked_line_count(workspace, path)
    additions += untracked_additions

    if untracked_files:
        note = f"\n untracked files | {len(untracked_files)} files, {untracked_additions} +\n"
        diff_stat = (diff_stat + note).strip() if diff_stat else note.strip()

    clean = not porcelain_entries and not changed and not untracked_files
    return {
        "branch": branch[:200],
        "changed_files": changed[:500],
        "diff_stat": diff_stat[:8000],
        "additions": additions,
        "deletions": deletions,
        "clean": bool(clean),
    }
