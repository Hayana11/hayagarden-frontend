#!/usr/bin/env python3
"""Regenerate summary_title for library posts using the diary-title prompt."""
import sqlite3
import sys

from tools import summary_title

TYPES = ('MEMORY', 'DIARY', 'FACT', 'THOUGHT', 'DREAM', 'DAILY_SUMMARY')


def main(limit=120, force=True, use_ai=True):
    conn = sqlite3.connect(summary_title.DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    placeholders = ','.join('?' * len(TYPES))
    where = 'COALESCE(resolved, 0) = 0'
    if not force:
        where += " AND (summary_title IS NULL OR TRIM(summary_title) = '')"
    rows = conn.execute(
        f"""SELECT id, content FROM posts
            WHERE type IN ({placeholders}) AND {where}
            ORDER BY id DESC LIMIT ?""",
        (*TYPES, limit),
    ).fetchall()
    updated = 0
    for row in rows:
        title = summary_title.generate_summary_title(row['content'] or '', use_ai=use_ai)
        conn.execute('UPDATE posts SET summary_title=? WHERE id=?', (title, row['id']))
        updated += 1
        print(f'#{row["id"]} -> {title}')
    conn.commit()
    conn.close()
    print(f'done, updated {updated}')


if __name__ == '__main__':
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    main(limit=limit, force=True, use_ai=True)
