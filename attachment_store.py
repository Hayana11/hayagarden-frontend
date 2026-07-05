"""附件间接层 + 生命周期。

- 图片不再直接暴露 /static 路径，而是纳入 attachment 管理，得到一个短 id。
  聊天/工具结果里携带的是 `attachment://<id>`，客户端拿 id 去 /api/attachments/<id> 取。
- 物理文件放在 ATTACH_DIR（不在 static 里，只能经 API 取），id 是唯一句柄。
- 生命周期：只保留最近 KEEP_N 张、且不超过 KEEP_DAYS 天，其余连文件带记录一起删。
- 独立数据库（attachments.db），不碰 memories.db —— 附件是易失数据。
"""
import os
import time
import sqlite3
import secrets

DB_PATH    = '/opt/frontend/attachments.db'
ATTACH_DIR = '/opt/frontend/attachments'
KEEP_N     = 30      # 最多保留最近 30 张
KEEP_DAYS  = 7       # 且不超过 7 天


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _init():
    os.makedirs(ATTACH_DIR, exist_ok=True)
    c = _conn()
    c.execute('''CREATE TABLE IF NOT EXISTS attachments (
        id         TEXT PRIMARY KEY,
        filename   TEXT NOT NULL,
        kind       TEXT DEFAULT 'image',
        mime       TEXT DEFAULT 'image/png',
        created_at DATETIME DEFAULT (datetime('now','+8 hours'))
    )''')
    c.commit()
    c.close()


_init()


def save(src_path, kind='image', mime='image/png'):
    """把一个已生成的文件纳入 attachment 管理，返回短 id。
    src_path 会被移动（rename）进 ATTACH_DIR，改名为 <id><ext>。"""
    _init()
    aid = secrets.token_hex(4)  # 8 位十六进制
    ext = os.path.splitext(src_path)[1] or '.png'
    fname = aid + ext
    dst = os.path.join(ATTACH_DIR, fname)
    os.replace(src_path, dst)  # 同盘 rename，原子
    c = _conn()
    c.execute('INSERT INTO attachments (id, filename, kind, mime) VALUES (?,?,?,?)',
              (aid, fname, kind, mime))
    c.commit()
    c.close()
    prune()
    return aid


def get(aid):
    """返回 {'id','filename','kind','mime','created_at'} 或 None。"""
    try:
        c = _conn()
        row = c.execute('SELECT * FROM attachments WHERE id=?', (aid,)).fetchone()
        c.close()
        return dict(row) if row else None
    except Exception:
        return None


def prune():
    """保留最近 KEEP_N 张且不超过 KEEP_DAYS 天，其余删除（记录+文件）。
    rowid 单调递增，按它排序即插入顺序。"""
    try:
        c = _conn()
        rows = c.execute('SELECT rowid, id, filename FROM attachments ORDER BY rowid DESC').fetchall()
        old = c.execute(
            "SELECT rowid FROM attachments WHERE created_at < datetime('now','+8 hours',?)",
            ('-%d days' % KEEP_DAYS,)).fetchall()
        old_ids = {r['rowid'] for r in old}
        doomed = []
        for i, r in enumerate(rows):
            if i >= KEEP_N or r['rowid'] in old_ids:
                doomed.append((r['rowid'], r['filename']))
        for rid, fname in doomed:
            try:
                os.remove(os.path.join(ATTACH_DIR, fname))
            except OSError:
                pass
            c.execute('DELETE FROM attachments WHERE rowid=?', (rid,))
        c.commit()
        c.close()
        return len(doomed)
    except Exception:
        return 0
