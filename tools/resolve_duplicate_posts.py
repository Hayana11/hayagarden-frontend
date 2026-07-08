#!/usr/bin/env python3.11
"""一次性：把语义重复的 posts 旧条目标为 resolved=1（保留较新 id）。"""
import sqlite3
import sys

sys.path.insert(0, '/opt/frontend')
from tools.memory_tool import is_near_duplicate, normalize_content

DB = '/opt/frontend/memories.db'
TYPES = ('FACT', 'MEMORY', 'DIARY', 'THOUGHT', 'DAILY_SUMMARY')


def main(dry_run=True):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, type, content FROM posts "
        "WHERE type IN ({}) AND COALESCE(resolved,0)=0 ORDER BY id DESC".format(
            ','.join('?' * len(TYPES))),
        TYPES,
    ).fetchall()
    resolved_ids = []
    seen = []  # (id, norm) newest first
    for row in rows:
        norm = normalize_content(row['content'])
        if not norm:
            continue
        if any(is_near_duplicate(norm, s[1]) for s in seen):
            resolved_ids.append(row['id'])
            continue
        seen.append((row['id'], norm))
    print('candidates to resolve:', len(resolved_ids), resolved_ids[:20])
    if not dry_run and resolved_ids:
        conn.executemany('UPDATE posts SET resolved=1 WHERE id=?', [(i,) for i in resolved_ids])
        conn.commit()
        print('resolved', len(resolved_ids))
    conn.close()


if __name__ == '__main__':
    main(dry_run='--apply' not in sys.argv)
