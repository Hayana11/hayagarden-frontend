"""Read-only git summary for the fixed Nexus workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from nexus_paths import NexusPathError, assert_path_inside

_GIT_TIMEOUT = 15


def _run_git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    assert_path_inside(workspace, workspace)
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            errors="replace",
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


def _stdout(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout or ""


def _ensure_git_worktree(workspace: Path) -> None:
    proc = _run_git(workspace, ["rev-parse", "--is-inside-work-tree"])
    if proc.returncode != 0 or _stdout(proc).strip().lower() != "true":
        detail = (_stdout(proc) or proc.stderr or "").strip()[:200]
        raise NexusPathError("not_a_git_worktree", detail or "not a git worktree")


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


def _untracked_line_count(workspace: Path, rel: str) -> int:
    path = workspace / rel
    try:
        if not path.is_file():
            return 0
        # Cap read to avoid huge files dominating the summary.
        data = path.read_bytes()[:512_000]
        if not data:
            return 0
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
    except OSError:
        return 0


def git_summary(workspace: Path) -> dict[str, Any]:
    """Return the frozen six-field git summary.

    Covers staged + unstaged (via diff against HEAD) and untracked (via porcelain).
    Raises NexusPathError when the workspace is not a git worktree — never reports
    clean=true for a non-repo.
    """
    _ensure_git_worktree(workspace)

    branch_proc = _run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = _stdout(branch_proc).strip() or "HEAD"

    porcelain = _stdout(_run_git(workspace, ["status", "--porcelain"]))
    # HEAD-relative covers both staged and unstaged tracked changes.
    diff_stat = _stdout(_run_git(workspace, ["diff", "HEAD", "--stat"])).rstrip()
    numstat = _stdout(_run_git(workspace, ["diff", "HEAD", "--numstat"]))
    name_only = _stdout(_run_git(workspace, ["diff", "HEAD", "--name-only"]))

    changed: list[str] = []
    for line in name_only.splitlines():
        name = line.strip()
        if name and name not in changed:
            changed.append(name)

    additions, deletions = _parse_numstat(numstat)

    untracked_additions = 0
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        name = line[3:].strip()
        if " -> " in name:
            name = name.split(" -> ", 1)[-1].strip()
        if not name:
            continue
        if name not in changed:
            changed.append(name)
        if status == "??":
            untracked_additions += _untracked_line_count(workspace, name)

    additions += untracked_additions

    # Append a compact untracked note to diff_stat when needed (field name frozen).
    if untracked_additions and "??" in porcelain:
        note = f"\n untracked files | {untracked_additions} +\n"
        diff_stat = (diff_stat + note).strip() if diff_stat else note.strip()

    clean = not porcelain.strip() and not changed
    return {
        "branch": branch[:200],
        "changed_files": changed[:500],
        "diff_stat": diff_stat[:8000],
        "additions": additions,
        "deletions": deletions,
        "clean": bool(clean),
    }
