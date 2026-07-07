#!/usr/bin/env python3
"""Backfill posts.summary_title for library rows missing a title."""
import sqlite3

from tools import summary_title

DB = summary_title.DB_PATH
LIBRARY_TYPES = ('MEMORY', 'DIARY', 'FACT', 'THOUGHT', 'DREAM', 'DAILY_SUMMARY')


def main(limit=120, use_ai=True):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    placeholders = ','.join('?' * len(LIBRARY_TYPES))
    rows = conn.execute(
        f"""SELECT id, content FROM posts
            WHERE type IN ({placeholders})
              AND COALESCE(resolved, 0) = 0
              AND (summary_title IS NULL OR TRIM(summary_title) = '')
            ORDER BY id DESC
            LIMIT ?""",
        (*LIBRARY_TYPES, limit),
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
    main()
