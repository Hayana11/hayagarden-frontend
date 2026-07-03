#!/usr/bin/env python3
"""记忆系统 M1 迁移：posts 表加 recall_count / last_recalled_at 两列。
家规#2：迁移前先备份整个 memories.db。幂等，跑多次无害。"""
import shutil, sqlite3, datetime, os

DB = '/opt/frontend/memories.db'

stamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
backup = f'/opt/frontend/memories.db.bak-memv2-{stamp}'
shutil.copy2(DB, backup)
print(f'备份完成: {backup} ({os.path.getsize(backup)} bytes)')

conn = sqlite3.connect(DB)
for col, defn in [('recall_count', 'INTEGER DEFAULT 0'),
                  ('last_recalled_at', 'TEXT')]:
    try:
        conn.execute(f'ALTER TABLE posts ADD COLUMN {col} {defn}')
        print(f'已加列: {col}')
    except Exception as e:
        print(f'跳过 {col}: {e}')
conn.commit()

cols = [r[1] for r in conn.execute('PRAGMA table_info(posts)').fetchall()]
assert 'recall_count' in cols and 'last_recalled_at' in cols, '迁移失败'
print('迁移验证通过, posts 列:', ', '.join(cols))
conn.close()
