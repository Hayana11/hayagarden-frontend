#!/usr/bin/env python3.11
"""迁移：新建 memory_vectors 表（记忆升级·方案二A）。

家规#2：动 memories.db 结构必须迁移脚本 + 备份先行——本脚本先备份再建表。
只新增表，不改任何已有表；幂等，重复跑无害。
"""
import datetime
import shutil
import sqlite3
import sys

DB = '/opt/frontend/memories.db'


def run():
    ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y%m%d%H%M%S')
    conn = sqlite3.connect(DB, timeout=10)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_vectors'").fetchone()
    if exists:
        print('memory_vectors already exists, nothing to do')
        conn.close()
        return
    conn.close()

    bak = DB + '.bak-memvec-' + ts
    shutil.copy2(DB, bak)
    print('backup: ' + bak)

    conn = sqlite3.connect(DB, timeout=10)
    conn.execute('''CREATE TABLE memory_vectors (
        post_id INTEGER PRIMARY KEY,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        vec BLOB NOT NULL,
        updated_at TEXT
    )''')
    conn.commit()
    conn.close()
    print('memory_vectors created')


if __name__ == '__main__':
    sys.exit(run())
