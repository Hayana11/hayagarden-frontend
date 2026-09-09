#!/usr/bin/env python3
"""Read-only historical replay for Continuity Compression R1.

This diagnostic opens the selected SQLite database with ``mode=ro`` and never
writes continuity artifacts. It exists to prove the source contract against
real transcript structure before any production consumer is connected.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import quote
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from continuity.coverage import validate_exact_coverage
from chat.daily_context import is_formal_chat_message
from continuity.sources import (
    build_source_members,
    derive_autonomous_events,
    derive_completed_turns,
    enumerate_candidate_source_refs,
    is_incomplete_source_row,
)

KNOWN_COLUMNS = (
    'id', 'author', 'content', 'thinking', 'created_at', 'tool_calls',
    'branches', 'branch_idx', 'cache_info', 'source_kind', 'attachments',
    'image_url', 'file_url', 'file_name',
)


def _open_ro(db_path: str) -> sqlite3.Connection:
    path = str(Path(db_path).resolve())
    conn = sqlite3.connect(f'file:{quote(path)}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, *, days: int) -> list[dict]:
    cols = {str(row[1]) for row in conn.execute('PRAGMA table_info(chat_messages)')}
    selected = [name for name in KNOWN_COLUMNS if name in cols]
    required = {'id', 'author', 'content', 'created_at'}
    if not required.issubset(cols):
        missing = ', '.join(sorted(required - cols))
        raise RuntimeError(f'chat_messages missing required columns: {missing}')
    since = (
        dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
        - dt.timedelta(days=max(1, int(days)))
    ).strftime('%Y-%m-%d %H:%M:%S')
    result = conn.execute(
        f"SELECT {', '.join(selected)} FROM chat_messages "
        'WHERE created_at >= ? ORDER BY id ASC',
        (since,),
    ).fetchall()
    return [dict(row) for row in result]


def replay(db_path: str, *, days: int = 30) -> dict:
    conn = _open_ro(db_path)
    try:
        rows = _rows(conn, days=days)
    finally:
        conn.close()

    turns = derive_completed_turns(rows)
    events = derive_autonomous_events(rows)
    members = build_source_members(turns, events)
    # Enumerate expected units from raw rows in a separate pass.  Comparing
    # against the already-derived turns/events would make coverage self-validating.
    expected_refs = enumerate_candidate_source_refs(rows)
    report = validate_exact_coverage(
        members,
        expected_source_refs=expected_refs,
    )

    formal_users = sum(
        1 for row in rows
        if str(row.get('author') or '').strip().lower() in {'hayana', 'haya', 'user'}
        and is_formal_chat_message(row)
    )
    explicit_incomplete = 0
    for row in rows:
        if (
            str(row.get('author') or '').strip().lower()
            in {'assistant', 'fyodor', 'claude'}
            and is_incomplete_source_row(row)
        ):
            explicit_incomplete += 1

    by_day = Counter(turn.started_at[:10] for turn in turns if turn.started_at)
    wake_by_day = Counter(event.created_at[:10] for event in events if event.created_at)
    return {
        'days': int(days),
        'rows_read': len(rows),
        'formal_user_starts': formal_users,
        'completed_turns': len(turns),
        'explicit_incomplete_assistant_rows': explicit_incomplete,
        'canonical_autonomous_events': len(events),
        'coverage_valid': report.valid,
        'coverage_issue_codes': sorted({issue.code for issue in report.issues}),
        'source_hash': report.source_hash,
        'completed_turns_by_day': dict(sorted(by_day.items())),
        'autonomous_events_by_day': dict(sorted(wake_by_day.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='/opt/frontend/memories.db')
    parser.add_argument('--days', type=int, default=30)
    args = parser.parse_args()
    result = replay(args.db, days=args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result['coverage_valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
