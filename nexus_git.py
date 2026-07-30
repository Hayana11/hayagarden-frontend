"""Read-only git summary for the fixed Nexus workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from nexus_paths import NexusPathError, assert_path_inside

_GIT_TIMEOUT = 15


def _run_git(workspace: Path, args: list[str]) -> str:
    assert_path_inside(workspace, workspace)
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd,
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
    if proc.returncode not in (0, 1):
        # 1 can occur for empty diffs in some git versions; treat other codes as soft empty.
        stderr = (proc.stderr or "").strip()[:200]
        if "not a git repository" in stderr.lower():
            return ""
        return ""
    return proc.stdout or ""


def git_summary(workspace: Path) -> dict[str, Any]:
    """Return the frozen six-field git summary. Never runs user shell strings."""
    branch = _run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"]).strip() or "HEAD"
    porcelain = _run_git(workspace, ["status", "--porcelain"])
    diff_stat = _run_git(workspace, ["diff", "--stat"]).rstrip()
    numstat = _run_git(workspace, ["diff", "--numstat"])
    name_only = _run_git(workspace, ["diff", "--name-only"])

    changed: list[str] = []
    for line in name_only.splitlines():
        name = line.strip()
        if name and name not in changed:
            changed.append(name)
    # Also include untracked / staged names from porcelain when diff is empty.
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        name = line[3:].strip()
        if " -> " in name:
            name = name.split(" -> ", 1)[-1].strip()
        if name and name not in changed:
            changed.append(name)

    additions = 0
    deletions = 0
    for line in numstat.splitlines():
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

    clean = not porcelain.strip() and not changed
    return {
        "branch": branch[:200],
        "changed_files": changed[:500],
        "diff_stat": diff_stat[:8000],
        "additions": additions,
        "deletions": deletions,
        "clean": bool(clean),
    }
