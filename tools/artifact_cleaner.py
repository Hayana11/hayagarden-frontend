#!/usr/bin/env python3.11
"""Assistant Artifact lifecycle cleaner — fail-conservative.

Referenced Artifacts (present in chat_messages.tool_calls) are NEVER
auto-deleted. Cap may only remove UNREFERENCED rows+files.

Persisted reference shapes (from gateway api_relay + CC result persistence):

  tool_calls JSON array elements may carry either or both of:

    {"name":"create_html","args":{...},"result":"{\\"artifact\\":{\\"id\\":7,...}}",
     "success":true,"artifact":{"id":7,"type":"html","title":"...","size":123}}

  - Prefer top-level ``artifact.id`` (api_relay lift).
  - Else parse ``result`` JSON string for ``artifact.id`` (CC / legacy).

If any non-empty tool_calls blob cannot be reliably parsed, auto-deletion of
UNREFERENCED artifacts is skipped for that run (physical orphans without a
DB row remain eligible — they cannot be referenced by artifact id).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from typing import Any, Optional

if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')

import artifact_store
import config_store as _cfg

DB_PATH = artifact_store.DB_PATH
STORE_DIR = artifact_store.STORE_DIR
DEFAULT_ORPHAN_GRACE_MIN = 1440  # 24h
DEFAULT_CAP_MB = 500


def _log(msg: str) -> None:
    import datetime
    ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime(
        '%Y-%m-%d %H:%M:%S'
    )
    line = '[%s] [artifact] %s\n' % (ts, msg)
    sys.stdout.write(line)
    try:
        open('/var/log/file_cleaner.log', 'a').write(line)
    except Exception:
        pass


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _coerce_artifact_id(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def extract_artifact_ids_from_tool_calls(tool_calls_raw: Any) -> tuple[set[int], bool]:
    """Return (ids, ok).

    ``ok=False`` means the blob was non-empty but not reliably parseable —
    callers must treat the scan as uncertain (fail-conservative).
    """
    if tool_calls_raw is None:
        return set(), True
    if isinstance(tool_calls_raw, (bytes, bytearray)):
        tool_calls_raw = tool_calls_raw.decode('utf-8', errors='replace')

    if isinstance(tool_calls_raw, list):
        data = tool_calls_raw
    elif isinstance(tool_calls_raw, str):
        text = tool_calls_raw.strip()
        if not text or text in ('[]', 'null', 'None'):
            return set(), True
        try:
            data = json.loads(text)
        except Exception:
            return set(), False
    else:
        return set(), False

    if not isinstance(data, list):
        return set(), False

    ids: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            # Non-object entry — cannot prove absence of a nested artifact ref.
            return set(), False
        aid = None
        art = item.get('artifact')
        if isinstance(art, dict):
            aid = _coerce_artifact_id(art.get('id'))
        if aid is None:
            result = item.get('result')
            if isinstance(result, str) and result.strip():
                try:
                    parsed = json.loads(result)
                except Exception:
                    # Result present but not JSON — if this looks like a create_*
                    # tool we cannot prove it is not an artifact ref.
                    name = str(item.get('name') or '')
                    if name.startswith('create_'):
                        return set(), False
                else:
                    if isinstance(parsed, dict) and isinstance(parsed.get('artifact'), dict):
                        aid = _coerce_artifact_id(parsed['artifact'].get('id'))
                    elif isinstance(parsed, dict) and 'artifact' in parsed:
                        return set(), False
            elif isinstance(result, dict) and isinstance(result.get('artifact'), dict):
                aid = _coerce_artifact_id(result['artifact'].get('id'))
        if aid is not None:
            ids.add(aid)
    return ids, True


def scan_referenced_artifact_ids(conn: sqlite3.Connection) -> tuple[set[int], bool]:
    """Scan chat_messages.tool_calls. Returns (ids, scan_certain)."""
    rows = conn.execute(
        "SELECT id, tool_calls FROM chat_messages "
        "WHERE tool_calls IS NOT NULL AND TRIM(tool_calls) != ''"
    ).fetchall()
    referenced: set[int] = set()
    certain = True
    for row in rows:
        ids, ok = extract_artifact_ids_from_tool_calls(row['tool_calls'])
        if not ok:
            certain = False
            continue
        referenced |= ids
    return referenced, certain


def clean_broken_rows(conn: sqlite3.Connection) -> int:
    """Remove artifacts rows whose physical file is missing."""
    removed = 0
    rows = conn.execute('SELECT id, filename FROM artifacts').fetchall()
    for row in rows:
        fpath = artifact_store._artifact_path(row['filename'])
        if fpath is None or not fpath.is_file():
            status = artifact_store.delete(row['id'], allow_stale_metadata=True)
            if status in ('deleted', 'stale_cleared'):
                removed += 1
    if removed:
        _log('broken metadata rows cleared: %d' % removed)
    return removed


def clean_physical_orphans(grace_min: int) -> int:
    """Files under STORE_DIR with no artifacts.filename row, past grace."""
    if not os.path.isdir(STORE_DIR):
        return 0
    conn = _db()
    try:
        known = {
            str(r['filename'])
            for r in conn.execute('SELECT filename FROM artifacts').fetchall()
        }
    finally:
        conn.close()

    now = time.time()
    removed = 0
    for name in os.listdir(STORE_DIR):
        if name in known:
            continue
        fpath = artifact_store._artifact_path(name)
        if fpath is None or not fpath.is_file():
            continue
        try:
            if now - fpath.stat().st_mtime < grace_min * 60:
                continue
            fpath.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        _log('physical orphans removed: %d' % removed)
    return removed


def clean_unreferenced(
    conn: sqlite3.Connection,
    referenced: set[int],
    grace_min: int,
    *,
    scan_certain: bool,
) -> int:
    """Delete UNREFERENCED artifacts older than grace. No-op if scan uncertain."""
    if not scan_certain:
        _log('skip unreferenced cleanup: tool_calls scan uncertain')
        return 0

    now = time.time()
    removed = 0
    rows = conn.execute(
        'SELECT id, filename, created_at FROM artifacts'
    ).fetchall()
    for row in rows:
        aid = int(row['id'])
        if aid in referenced:
            continue
        fpath = artifact_store._artifact_path(row['filename'])
        # Prefer file mtime; fall back to allowing delete when file missing
        # (broken row handled elsewhere, but safe here too).
        age_ok = True
        if fpath is not None and fpath.is_file():
            try:
                age_ok = (now - fpath.stat().st_mtime) >= grace_min * 60
            except OSError:
                age_ok = True
        if not age_ok:
            continue
        status = artifact_store.delete(aid, allow_stale_metadata=True)
        if status in ('deleted', 'stale_cleared'):
            removed += 1
    if removed:
        _log('unreferenced removed: %d' % removed)
    return removed


def enforce_cap(
    conn: sqlite3.Connection,
    referenced: set[int],
    cap_mb: int,
    *,
    scan_certain: bool,
) -> tuple[int, bool]:
    """Delete oldest UNREFERENCED until under cap.

    Returns (removed_count, retained_over_cap).
    Never deletes referenced artifacts.
    """
    if cap_mb <= 0:
        return 0, False
    if not scan_certain:
        _log('skip cap cleanup: tool_calls scan uncertain')
        return 0, False

    cap_bytes = cap_mb * 1024 * 1024
    rows = conn.execute(
        'SELECT id, filename, size, created_at FROM artifacts '
        'ORDER BY datetime(created_at) ASC, id ASC'
    ).fetchall()

    entries = []
    total = 0
    for row in rows:
        fpath = artifact_store._artifact_path(row['filename'])
        size = int(row['size'] or 0)
        if fpath is not None and fpath.is_file():
            try:
                size = fpath.stat().st_size
            except OSError:
                pass
        total += size
        entries.append((int(row['id']), size, int(row['id']) in referenced))

    if total <= cap_bytes:
        return 0, False

    removed = 0
    for aid, size, is_ref in entries:
        if total <= cap_bytes:
            break
        if is_ref:
            continue
        status = artifact_store.delete(aid, allow_stale_metadata=True)
        if status in ('deleted', 'stale_cleared'):
            total -= size
            removed += 1

    retained_over = total > cap_bytes
    if removed:
        _log('cap enforced (>%dMB): removed %d unreferenced' % (cap_mb, removed))
    if retained_over:
        _log(
            'WARNING retained-over-cap: only referenced artifacts remain '
            '(>%dMB); refusing to delete history' % cap_mb
        )
    return removed, retained_over


def run() -> dict:
    grace_min = _cfg.get_int('ARTIFACT_CLEAN_ORPHAN_GRACE_MIN', DEFAULT_ORPHAN_GRACE_MIN)
    cap_mb = _cfg.get_int('ARTIFACT_CLEAN_CAP_MB', DEFAULT_CAP_MB)
    conn = _db()
    try:
        referenced, certain = scan_referenced_artifact_ids(conn)
        broken = clean_broken_rows(conn)
        orphans = clean_physical_orphans(grace_min)
        unreferenced = clean_unreferenced(
            conn, referenced, grace_min, scan_certain=certain,
        )
        capped, retained_over = enforce_cap(
            conn, referenced, cap_mb, scan_certain=certain,
        )
        result = {
            'referenced': len(referenced),
            'scan_certain': certain,
            'broken': broken,
            'physical_orphans': orphans,
            'unreferenced': unreferenced,
            'cap_removed': capped,
            'retained_over_cap': retained_over,
        }
        if not any((broken, orphans, unreferenced, capped)):
            _log('nothing to clean (referenced=%d certain=%s)' % (
                len(referenced), certain,
            ))
        return result
    finally:
        conn.close()


if __name__ == '__main__':
    run()
