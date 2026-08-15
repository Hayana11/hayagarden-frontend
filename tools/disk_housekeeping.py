#!/usr/bin/env python3.11
"""Whitelist-only HayaGarden disk housekeeping.

The command is deliberately conservative: it has no cleanup-root CLI option,
does not call tmpfiles, and never treats an unknown path as disposable.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

try:  # pragma: no cover - exercised on Linux production
    import fcntl
except ImportError:  # pragma: no cover - Windows test fallback
    fcntl = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = Path('/tmp')
DEPLOY_LOCK = Path('/var/lock/hayagarden-frontend-deploy.lock')
HOUSEKEEPING_LOCK = Path('/var/lock/hayagarden-housekeeping.lock')
NPM_CACHE = Path('/root/.npm')
PIP_CACHE = Path('/root/.cache/pip')
PYTHON = '/usr/bin/python3.11'

# Every prefix below has a matching tempfile.mkdtemp(prefix=...) call in the
# Forge contract tests.  Do not broaden this tuple from production observations
# alone; unproven Round 1 names remain report-only.
TMP_ALLOWLIST_PREFIXES = (
    'forge-switch-',
    'forge-id-',
    'forge-post-',
    'forge-pswap-',
    'forge-dead-',
    'forge-vision-',
)


@dataclass(frozen=True)
class DiskStats:
    used_pct: float
    free_bytes: int
    inode_pct: float


@dataclass(frozen=True)
class Candidate:
    path: Path
    size_bytes: int
    age_seconds: int
    fingerprint: tuple[int, int, int, int]


@dataclass(frozen=True)
class Decision:
    candidate: Candidate | None
    reason: str = ''


@dataclass
class RunResult:
    tmp_candidates: int = 0
    tmp_deleted: int = 0
    tmp_reclaimed_bytes: int = 0
    cache_reclaimed_bytes: int = 0
    backup_reclaimed_bytes: int = 0
    result: str = 'PASS'
    errors: list[str] = field(default_factory=list)


class LockBackend:
    """Non-blocking flock backend; tests can inject a deterministic double."""

    def status(self, path: Path) -> str:
        if fcntl is None:
            return 'unknown'
        if not path.exists():
            return 'free'
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return 'unknown'
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'busy'
            except OSError:
                return 'unknown'
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            return 'free'
        finally:
            os.close(fd)

    def acquire(self, path: Path) -> int | None:
        if fcntl is None:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            try:
                os.close(fd)  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass
            return None
        except OSError:
            try:
                os.close(fd)  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass
            return None

    def release(self, handle: int | None) -> None:
        if handle is None:
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[union-attr]
        finally:
            os.close(handle)


def disk_stats(root: Path = Path('/')) -> DiskStats:
    info = os.statvfs(root)
    used = info.f_blocks - info.f_bfree
    used_pct = (used / info.f_blocks * 100.0) if info.f_blocks else 0.0
    free_bytes = info.f_bavail * info.f_frsize
    inode_used = info.f_files - info.f_ffree
    inode_pct = (inode_used / info.f_files * 100.0) if info.f_files else 0.0
    return DiskStats(round(used_pct, 1), int(free_bytes), round(inode_pct, 1))


def _allocated_bytes(info: os.stat_result) -> int:
    blocks = getattr(info, 'st_blocks', 0)
    return int(blocks * 512 if blocks else info.st_size)


def subtree_size(path: Path) -> int:
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        info = os.lstat(current)
        total += _allocated_bytes(info)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(current) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    return total


def _same_or_below(path: Path, root: Path) -> bool:
    path_text = os.path.abspath(os.fspath(path))
    root_text = os.path.abspath(os.fspath(root))
    try:
        return os.path.commonpath((path_text, root_text)) == root_text
    except ValueError:
        return False


def load_worktree_paths(repo_root: Path = REPO_ROOT) -> list[Path] | None:
    try:
        proc = subprocess.run(
            ['git', '-C', str(repo_root), 'worktree', 'list', '--porcelain'],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, UnicodeError):
        return None
    if proc.returncode != 0:
        return None
    paths: list[Path] = []
    for line in proc.stdout.splitlines():
        if line.startswith('worktree '):
            paths.append(Path(line.removeprefix('worktree ')))
    return paths


def _proc_target_references(path: Path, proc_root: Path = Path('/proc')) -> tuple[bool, bool]:
    """Return (referenced, uncertain) for cwd/root/fd process references."""
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        return False, True
    for process in processes:
        if not process.name.isdigit():
            continue
        links = [process / 'cwd', process / 'root']
        try:
            links.extend((process / 'fd').iterdir())
        except OSError:
            return False, True
        for link in links:
            try:
                target = os.readlink(link)
            except OSError:
                return False, True
            if not target.startswith('/'):
                continue
            if target.endswith(' (deleted)'):
                return False, True
            if _same_or_below(Path(target), path):
                return True, False
    return False, False


def _decode_mountinfo_path(value: str) -> str | None:
    if re.search(r'\\(?!040|011|012|134)', value):
        return None
    return (
        value.replace('\\040', ' ')
        .replace('\\011', '\t')
        .replace('\\012', '\n')
        .replace('\\134', '\\')
    )


def load_mount_points(mountinfo_path: Path = Path('/proc/self/mountinfo')) -> set[Path] | None:
    """Read Linux mount points; None means the boundary evidence is uncertain."""
    try:
        text = mountinfo_path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None
    points: set[Path] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        sections = line.split(' - ', 1)
        if len(sections) != 2:
            return None
        left, right = sections
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 3:
            return None
        decoded = _decode_mountinfo_path(left_fields[4])
        if not decoded or not decoded.startswith('/'):
            return None
        points.add(Path(os.path.normpath(decoded)))
    return points or None


def _validate_subtree(path: Path, tmp_root: Path) -> str | None:
    try:
        root_info = os.lstat(tmp_root)
        root_device = root_info.st_dev
        pending = [path]
        while pending:
            current = pending.pop()
            info = os.lstat(current)
            if info.st_dev != root_device:
                return 'cross-mount'
            if stat.S_ISLNK(info.st_mode):
                return 'symlink'
            if stat.S_ISSOCK(info.st_mode) or stat.S_ISCHR(info.st_mode):
                return 'special-object'
            if stat.S_ISBLK(info.st_mode) or stat.S_ISFIFO(info.st_mode):
                return 'special-object'
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                return 'special-object'
            if stat.S_ISDIR(info.st_mode):
                if current.name.startswith(('systemd-private-', 'snap-private-')):
                    return 'private-runtime-path'
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.name == '.git':
                            return 'git-marker'
                        pending.append(Path(entry.path))
    except OSError:
        return 'uncertain-subtree'
    return None


def evaluate_candidate(
    path: Path,
    *,
    tmp_root: Path = TMP_ROOT,
    worktree_paths: Sequence[Path] | None = None,
    proc_checker: Callable[[Path], tuple[bool, bool]] = _proc_target_references,
    mountinfo_loader: Callable[[], set[Path] | None] = load_mount_points,
    now: float | None = None,
    min_age_seconds: int = 24 * 60 * 60,
) -> Decision:
    now = time.time() if now is None else now
    if path.parent != tmp_root or not any(path.name.startswith(p) for p in TMP_ALLOWLIST_PREFIXES):
        return Decision(None, 'not-allowlisted')
    try:
        info = os.lstat(path)
    except OSError:
        return Decision(None, 'SKIP_UNCERTAIN:lstat')
    if stat.S_ISLNK(info.st_mode):
        return Decision(None, 'SKIP_UNCERTAIN:symlink')
    if not stat.S_ISDIR(info.st_mode):
        return Decision(None, 'SKIP_NON_DIRECTORY')
    if info.st_dev != os.lstat(tmp_root).st_dev:
        return Decision(None, 'SKIP_UNCERTAIN:cross-mount')
    age = int(max(0, now - info.st_mtime))
    if age < min_age_seconds:
        return Decision(None, 'too-young')
    if worktree_paths is None:
        return Decision(None, 'SKIP_UNCERTAIN:worktree-list')
    if any(_same_or_below(path, worktree) for worktree in worktree_paths):
        return Decision(None, 'SKIP_UNCERTAIN:git-worktree')
    mount_points = mountinfo_loader()
    if mount_points is None:
        return Decision(None, 'SKIP_UNCERTAIN:mountinfo')
    if any(_same_or_below(mount_point, path) for mount_point in mount_points):
        return Decision(None, 'SKIP_UNCERTAIN:mount-boundary')
    reason = _validate_subtree(path, tmp_root)
    if reason:
        return Decision(None, f'SKIP_UNCERTAIN:{reason}')
    referenced, uncertain = proc_checker(path)
    if uncertain:
        return Decision(None, 'SKIP_UNCERTAIN:proc-scan')
    if referenced:
        return Decision(None, 'SKIP_UNCERTAIN:active-proc-reference')
    try:
        size = subtree_size(path)
    except OSError:
        return Decision(None, 'SKIP_UNCERTAIN:size')
    fingerprint = (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns)
    return Decision(Candidate(path, size, age, fingerprint))


def discover_tmp_candidates(
    *,
    tmp_root: Path = TMP_ROOT,
    worktree_paths: Sequence[Path] | None = None,
    proc_checker: Callable[[Path], tuple[bool, bool]] = _proc_target_references,
    mountinfo_loader: Callable[[], set[Path] | None] = load_mount_points,
    now: float | None = None,
    emit: Callable[[str], None] = print,
) -> tuple[list[Decision], int]:
    decisions: list[Decision] = []
    count = 0
    try:
        children = list(tmp_root.iterdir())
    except OSError:
        return [Decision(None, 'SKIP_UNCERTAIN:tmp-list')], 0
    for child in children:
        if not any(child.name.startswith(p) for p in TMP_ALLOWLIST_PREFIXES):
            continue
        count += 1
        decision = evaluate_candidate(
            child,
            tmp_root=tmp_root,
            worktree_paths=worktree_paths,
            proc_checker=proc_checker,
            mountinfo_loader=mountinfo_loader,
            now=now,
        )
        decisions.append(decision)
        if decision.candidate is None:
            if decision.reason.startswith('SKIP_UNCERTAIN'):
                emit(f'SKIP_UNCERTAIN path={child} reason={decision.reason.split(":", 1)[1]}')
            elif decision.reason == 'SKIP_NON_DIRECTORY':
                emit(f'SKIP_NON_DIRECTORY path={child}')
    return decisions, count


def _revalidate(candidate: Candidate, **kwargs: object) -> Decision:
    current = evaluate_candidate(candidate.path, **kwargs)  # type: ignore[arg-type]
    if current.candidate is None:
        return Decision(None, 'SKIP_UNCERTAIN:revalidate')
    try:
        info = os.lstat(candidate.path)
    except OSError:
        return Decision(None, 'SKIP_UNCERTAIN:revalidate-lstat')
    current_fp = (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns)
    if current_fp != candidate.fingerprint:
        return Decision(None, 'SKIP_UNCERTAIN:changed-before-delete')
    return current


def clean_tmp(
    *,
    execute: bool,
    tmp_root: Path = TMP_ROOT,
    worktree_paths: Sequence[Path] | None = None,
    proc_checker: Callable[[Path], tuple[bool, bool]] = _proc_target_references,
    mountinfo_loader: Callable[[], set[Path] | None] = load_mount_points,
    now: float | None = None,
    emit: Callable[[str], None] = print,
) -> tuple[int, int, int, list[str]]:
    decisions, count = discover_tmp_candidates(
        tmp_root=tmp_root,
        worktree_paths=worktree_paths,
        proc_checker=proc_checker,
        mountinfo_loader=mountinfo_loader,
        now=now,
        emit=emit,
    )
    deleted = 0
    reclaimed = 0
    errors: list[str] = []
    for decision in decisions:
        if decision.candidate is None:
            continue
        candidate = decision.candidate
        if not execute:
            emit(
                f'WOULD_DELETE_TMP path={candidate.path} '
                f'size={candidate.size_bytes} age={candidate.age_seconds}'
            )
            continue
        second = _revalidate(
            candidate,
            tmp_root=tmp_root,
            worktree_paths=worktree_paths,
            proc_checker=proc_checker,
            mountinfo_loader=mountinfo_loader,
            now=now,
        )
        if second.candidate is None:
            emit(f'SKIP_UNCERTAIN path={candidate.path} reason=revalidate')
            continue
        try:
            info = os.lstat(candidate.path)
            if not stat.S_ISDIR(info.st_mode):
                emit(f'SKIP_NON_DIRECTORY path={candidate.path}')
                continue
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(candidate.path)
        except OSError as exc:
            errors.append(f'{candidate.path}: {exc}')
            emit(f'ERROR_DELETE_TMP path={candidate.path} error={exc}')
            continue
        deleted += 1
        reclaimed += candidate.size_bytes
        emit(f'DELETED_TMP path={candidate.path} size={candidate.size_bytes}')
    return count, deleted, reclaimed, errors


def _run_command(
    command: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(command, check=False, capture_output=True, text=True)


def _command_text(result: subprocess.CompletedProcess[str]) -> str:
    return f'{result.stdout}\n{result.stderr}'.strip()


def _path_size(path: Path) -> int:
    try:
        return subtree_size(path)
    except OSError:
        return 0


def _npm_cache_path(runner: Callable[..., subprocess.CompletedProcess[str]]) -> Path | None:
    result = _run_command(['npm', 'config', 'get', 'cache'], runner=runner)
    if result.returncode != 0:
        return None
    value = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''
    return Path(value).expanduser().resolve() if value else None


def _pip_cache_path(runner: Callable[..., subprocess.CompletedProcess[str]]) -> Path | None:
    result = _run_command([PYTHON, '-m', 'pip', 'cache', 'dir'], runner=runner)
    if result.returncode != 0:
        return None
    value = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''
    return Path(value).expanduser().resolve() if value else None


def _npx_cache_supported(runner: Callable[..., subprocess.CompletedProcess[str]]) -> bool:
    result = _run_command(['npm', 'cache', '--help'], runner=runner)
    text = _command_text(result).lower()
    return result.returncode == 0 and re.search(r'\bcache\s+npx\s+rm\b', text) is not None


def weekly_cache_actions(
    *,
    execute: bool,
    npm_cache_path: Path = NPM_CACHE,
    pip_cache_path: Path = PIP_CACHE,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    emit: Callable[[str], None] = print,
) -> tuple[int, list[str]]:
    errors: list[str] = []
    npm_path = _npm_cache_path(runner)
    if npm_path != npm_cache_path:
        emit(f'REFUSE_UNEXPECTED_NPM_CACHE_PATH path={npm_path or "unknown"}')
        return 0, ['unexpected npm cache path']
    pip_path = _pip_cache_path(runner)
    if pip_path != pip_cache_path:
        emit(f'REFUSE_UNEXPECTED_PIP_CACHE_PATH path={pip_path or "unknown"}')
        return 0, ['unexpected pip cache path']
    before = _path_size(npm_cache_path) + _path_size(pip_cache_path)
    if not _npx_cache_supported(runner):
        emit('SKIP_NPX_UNSUPPORTED')
    elif execute:
        result = _run_command(['npm', 'cache', 'npx', 'rm', '--cache', str(npm_cache_path)], runner=runner)
        if result.returncode != 0:
            errors.append('npx cache rm failed')
            emit(f'ERROR_NPX_RM {_command_text(result)}')
        else:
            emit('NPX_REMOVED')
    else:
        emit('WOULD_NPX_RM')
    if execute:
        result = _run_command(['npm', 'cache', 'verify', '--cache', str(npm_cache_path)], runner=runner)
        if result.returncode != 0:
            errors.append('npm cache verify failed')
            emit(f'ERROR_NPM_VERIFY {_command_text(result)}')
    else:
        emit('WOULD_NPM_VERIFY')
    if execute:
        result = _run_command([PYTHON, '-m', 'pip', 'cache', 'purge'], runner=runner)
        if result.returncode != 0:
            errors.append('pip cache purge failed')
            emit(f'ERROR_PIP_PURGE {_command_text(result)}')
    else:
        emit(f'WOULD_PIP_PURGE size={_path_size(pip_cache_path)}')
    after = _path_size(npm_cache_path) + _path_size(pip_cache_path)
    return max(0, before - after) if execute else 0, errors


def backup_retention_action(
    *,
    execute: bool,
    repo_root: Path = REPO_ROOT,
    backup_root: Path = Path('/opt/backups/frontend'),
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    emit: Callable[[str], None] = print,
) -> tuple[int, bool, list[str]]:
    before = _path_size(backup_root)
    command = [PYTHON, str(repo_root / 'tools' / 'backup_retention.py')]
    if not execute:
        command.append('--dry-run')
    result = _run_command(command, runner=runner)
    text = _command_text(result)
    if text:
        emit(text)
    disk_alert = 'DISK ALERT' in text
    if result.returncode not in (0, 1) or (result.returncode == 1 and not disk_alert):
        return 0, disk_alert, ['backup retention failed']
    after = _path_size(backup_root)
    return max(0, before - after) if execute else 0, disk_alert, []


def _print_start(mode: str, dry_run: bool, stats: DiskStats, locks: dict[str, str], emit: Callable[[str], None]) -> None:
    emit('HOUSEKEEPING_START')
    emit(f'mode={mode}')
    emit(f'dry_run={str(dry_run).lower()}')
    emit(f'disk_before_pct={stats.used_pct}')
    emit(f'disk_before_free_bytes={stats.free_bytes}')
    emit(f'inode_before_pct={stats.inode_pct}')
    emit(f'deploy_lock={locks["deploy"]}')
    emit(f'housekeeping_lock={locks["housekeeping"]}')


def _print_end(result: RunResult, stats: DiskStats, emit: Callable[[str], None]) -> None:
    emit('HOUSEKEEPING_END')
    emit(f'tmp_candidates={result.tmp_candidates}')
    emit(f'tmp_deleted={result.tmp_deleted}')
    emit(f'tmp_reclaimed_bytes={result.tmp_reclaimed_bytes}')
    emit(f'cache_reclaimed_bytes={result.cache_reclaimed_bytes}')
    emit(f'backup_reclaimed_bytes={result.backup_reclaimed_bytes}')
    emit(f'disk_after_pct={stats.used_pct}')
    emit(f'disk_after_free_bytes={stats.free_bytes}')
    emit(f'result={result.result}')


def run_housekeeping(
    mode: str,
    *,
    dry_run: bool,
    repo_root: Path = REPO_ROOT,
    tmp_root: Path = TMP_ROOT,
    worktree_paths: Sequence[Path] | None = None,
    proc_checker: Callable[[Path], tuple[bool, bool]] = _proc_target_references,
    mountinfo_loader: Callable[[], set[Path] | None] = load_mount_points,
    lock_backend: LockBackend | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    stats_provider: Callable[[], DiskStats] = disk_stats,
    npm_cache_path: Path = NPM_CACHE,
    pip_cache_path: Path = PIP_CACHE,
    backup_root: Path = Path('/opt/backups/frontend'),
    emit: Callable[[str], None] = print,
) -> RunResult:
    lock_backend = lock_backend or LockBackend()
    before = stats_provider()
    locks = {
        'deploy': lock_backend.status(DEPLOY_LOCK),
        'housekeeping': lock_backend.status(HOUSEKEEPING_LOCK),
    }
    _print_start(mode, dry_run, before, locks, emit)
    outcome = RunResult()
    if not dry_run:
        housekeeping_handle = lock_backend.acquire(HOUSEKEEPING_LOCK)
        if housekeeping_handle is None:
            emit('SKIP_ACTIVE_LOCK')
            outcome.result = 'SKIPPED'
            _print_end(outcome, stats_provider(), emit)
            return outcome
        deploy_handle = lock_backend.acquire(DEPLOY_LOCK)
        if deploy_handle is None:
            lock_backend.release(housekeeping_handle)
            emit('SKIP_ACTIVE_LOCK')
            outcome.result = 'SKIPPED'
            _print_end(outcome, stats_provider(), emit)
            return outcome
    else:
        housekeeping_handle = deploy_handle = None
    try:
        if mode == 'daily':
            worktrees = list(worktree_paths) if worktree_paths is not None else load_worktree_paths(repo_root)
            count, deleted, reclaimed, errors = clean_tmp(
                execute=not dry_run,
                tmp_root=tmp_root,
                worktree_paths=worktrees,
                proc_checker=proc_checker,
                mountinfo_loader=mountinfo_loader,
                emit=emit,
            )
            outcome.tmp_candidates = count
            outcome.tmp_deleted = deleted
            outcome.tmp_reclaimed_bytes = reclaimed
            outcome.errors.extend(errors)
            reclaimed_backup, disk_alert, errors = backup_retention_action(
                execute=not dry_run,
                repo_root=repo_root,
                backup_root=backup_root,
                runner=runner,
                emit=emit,
            )
            outcome.backup_reclaimed_bytes = reclaimed_backup
            outcome.errors.extend(errors)
            if disk_alert and not outcome.errors:
                outcome.result = 'PASS_WITH_DISK_ALERT'
        elif mode == 'weekly':
            reclaimed_cache, errors = weekly_cache_actions(
                execute=not dry_run,
                npm_cache_path=npm_cache_path,
                pip_cache_path=pip_cache_path,
                runner=runner,
                emit=emit,
            )
            outcome.cache_reclaimed_bytes = reclaimed_cache
            outcome.errors.extend(errors)
        else:
            outcome.errors.append(f'unknown mode: {mode}')
        if outcome.errors:
            outcome.result = 'ERROR'
        elif outcome.result == 'PASS' and stats_provider().used_pct >= 90.0:
            outcome.result = 'PASS_WITH_DISK_ALERT'
    finally:
        if not dry_run:
            lock_backend.release(deploy_handle)
            lock_backend.release(housekeeping_handle)
    _print_end(outcome, stats_provider(), emit)
    return outcome


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run whitelist-only HayaGarden housekeeping.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--daily', action='store_true')
    mode.add_argument('--weekly', action='store_true')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--dry-run', action='store_true')
    action.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    args.mode = 'daily' if args.daily else 'weekly'
    args.dry_run = not args.execute
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_housekeeping(args.mode, dry_run=args.dry_run)
    return 0 if result.result in {'PASS', 'PASS_WITH_DISK_ALERT', 'SKIPPED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
