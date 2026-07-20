#!/usr/bin/env python3.11
"""Prune /opt/backups/frontend with anchor-safe retention rules.

Rules (union — a file is kept if any rule matches):
  - frontend-*.tar.gz: newest 3, plus 1/day for last 7 days, plus 1/monthly anchor
  - predeploy-runtime-*: newest 3
  - backup_anchors.txt: explicit protected basenames (never deleted)
  - manual dirs / .bak files: never scanned for deletion

Disk >= 80%: log alert only; never bypass anchor protection to delete more.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

FRONTEND_TAR_RE = re.compile(r'^frontend-(\d{8})-(\d{6})\.tar\.gz$')
RUNTIME_DIR_RE = re.compile(r'^predeploy-runtime-(\d{8})-(\d{6})$')

MANUAL_PREFIXES = (
    'moments-predeploy-',
    'pre-monopoly-',
    'manual-',
    'profile-page-',
    'profile-sync-',
    'dream-',
    'relay-',
    'pre-clean-',
    'pre-wake-',
    'moments-pagination-',
)


def _parse_ts(name: str, pattern: re.Pattern[str]) -> datetime | None:
    m = pattern.match(name)
    if not m:
        return None
    return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')


def load_anchors(anchors_file: Path) -> set[str]:
    if not anchors_file.exists():
        return set()
    names: set[str] = set()
    for line in anchors_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            names.add(line)
    return names


def keep_frontend_tars(
    names: list[str],
    now: datetime,
    *,
    deploy_keep: int = 3,
    daily_keep_days: int = 7,
) -> set[str]:
    parsed: list[tuple[datetime, str]] = []
    for name in names:
        ts = _parse_ts(name, FRONTEND_TAR_RE)
        if ts is not None:
            parsed.append((ts, name))
    parsed.sort(reverse=True)

    keep: set[str] = set()
    for _, name in parsed[:deploy_keep]:
        keep.add(name)

    cutoff = (now - timedelta(days=daily_keep_days - 1)).date()
    by_day: dict = {}
    for ts, name in parsed:
        if ts.date() >= cutoff:
            day = ts.date()
            if day not in by_day or ts > by_day[day][0]:
                by_day[day] = (ts, name)
    keep.update(name for _, name in by_day.values())

    by_month: dict = {}
    for ts, name in parsed:
        key = (ts.year, ts.month)
        if key not in by_month or ts > by_month[key][0]:
            by_month[key] = (ts, name)
    keep.update(name for _, name in by_month.values())

    return keep


def keep_runtime_dirs(names: list[str], *, keep_count: int = 3) -> set[str]:
    parsed: list[tuple[datetime, str]] = []
    for name in names:
        ts = _parse_ts(name, RUNTIME_DIR_RE)
        if ts is not None:
            parsed.append((ts, name))
    parsed.sort(reverse=True)
    return {name for _, name in parsed[:keep_count]}


def disk_usage_percent(path: str = '/') -> float:
    st = os.statvfs(path)
    total = st.f_blocks
    if not total:
        return 0.0
    used = st.f_blocks - st.f_bfree
    return used / total * 100.0


def prune(
    backup_dir: Path,
    anchors_file: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    anchors = load_anchors(anchors_file)

    tar_names = sorted(
        p.name for p in backup_dir.glob('frontend-*.tar.gz') if p.is_file()
    )
    runtime_names = sorted(
        p.name for p in backup_dir.iterdir()
        if p.is_dir() and p.name.startswith('predeploy-runtime-')
    )

    keep_tars = keep_frontend_tars(tar_names, now)
    keep_tars |= anchors & set(tar_names)
    keep_runtimes = keep_runtime_dirs(runtime_names)
    keep_runtimes |= anchors & set(runtime_names)

    deleted: list[tuple[str, str]] = []
    for name in tar_names:
        if name in keep_tars:
            continue
        path = backup_dir / name
        if dry_run:
            deleted.append(('tar', name))
        else:
            path.unlink()
            deleted.append(('tar', name))

    for name in runtime_names:
        if name in keep_runtimes:
            continue
        path = backup_dir / name
        if dry_run:
            deleted.append(('runtime', name))
        else:
            shutil.rmtree(path)
            deleted.append(('runtime', name))

    pct = disk_usage_percent('/')
    return {
        'keep_tars': sorted(keep_tars),
        'keep_runtimes': sorted(keep_runtimes),
        'deleted': deleted,
        'disk_pct': round(pct, 1),
        'disk_alert': pct >= 80.0,
        'dry_run': dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Prune frontend backups with anchor safety.')
    parser.add_argument('--backup-dir', default='/opt/backups/frontend')
    parser.add_argument(
        '--anchors-file',
        default=str(Path(__file__).resolve().parent / 'backup_anchors.txt'),
    )
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)

    result = prune(
        Path(args.backup_dir),
        Path(args.anchors_file),
        dry_run=args.dry_run,
    )

    mode = 'dry-run' if result['dry_run'] else 'pruned'
    print(
        f'backup_retention {mode}: keep {len(result["keep_tars"])} tar(s), '
        f'{len(result["keep_runtimes"])} runtime(s); '
        f'deleted {len(result["deleted"])} item(s); '
        f'disk {result["disk_pct"]}%'
    )
    if result['disk_alert']:
        print(
            'DISK ALERT: root filesystem >= 80% — review backups manually; '
            'anchors are never auto-deleted',
            file=sys.stderr,
        )
    for kind, name in result['deleted']:
        print(f'  deleted {kind}: {name}')
    return 1 if result['disk_alert'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
