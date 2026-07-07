#!/usr/bin/env python3
"""Add posts.summary_title column. Idempotent; backs up DB first."""
import datetime
import os
import shutil
import sqlite3

DB = '/opt/frontend/memories.db'


def main():
    stamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    backup = f'/opt/frontend/memories.db.bak-summary-title-{stamp}'
    shutil.copy2(DB, backup)
    print(f'backup: {backup} ({os.path.getsize(backup)} bytes)')

    conn = sqlite3.connect(DB)
    try:
        conn.execute('ALTER TABLE posts ADD COLUMN summary_title TEXT')
        print('added column summary_title')
    except sqlite3.OperationalError as e:
        print(f'skip summary_title: {e}')
    conn.commit()
    cols = [r[1] for r in conn.execute('PRAGMA table_info(posts)').fetchall()]
    assert 'summary_title' in cols, 'migration failed'
    print('ok, posts columns:', ', '.join(cols))
    conn.close()


if __name__ == '__main__':
    main()
