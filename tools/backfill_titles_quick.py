#!/usr/bin/env python3
import sqlite3
from tools import summary_title

TYPES = ('MEMORY', 'DIARY', 'FACT', 'THOUGHT', 'DREAM', 'DAILY_SUMMARY')
conn = sqlite3.connect('/opt/frontend/memories.db', timeout=60)
rows = conn.execute(
    f"SELECT id, content FROM posts WHERE type IN ({','.join('?'*len(TYPES))}) "
    "AND COALESCE(resolved,0)=0 AND (summary_title IS NULL OR TRIM(summary_title)='') "
    "ORDER BY id DESC LIMIT 120",
    TYPES,
).fetchall()
for pid, content in rows:
    conn.execute('UPDATE posts SET summary_title=? WHERE id=?', (summary_title.rule_summary_title(content or ''), pid))
conn.commit()
print('updated', len(rows))
conn.close()
