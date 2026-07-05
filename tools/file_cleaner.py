#!/usr/bin/env python3.11
"""用户上传文件的生命周期清理——每晚跑（cron 03:30）。

背景：/static/uploads/files/ 是用户发来的文件（file 收发功能引入），之前无任何清理，
会无限堆积吃磁盘。图片附件（attachment_store）有“最近30张/7天”，文件却没。

两档策略（都保守，宁可少删）：
  1. 孤儿文件：磁盘上存在但没有任何 chat_messages.file_url 引用的文件（上传了没发出、
     或消息被删/文件字段被清后残留的）——这些是纯垃圾，直接删。
     留 GRACE 宽限，避免误删刚上传还没入库的文件。
  2. 总量上限：若目录总占用超过 CAP_MB，从最老的开始删（删物理文件 + 清空对应消息的
     file 字段，消息本体保留，变回普通气泡），直到降到阀值下。

两档都可用 config_store 旋钮在线调：FILE_CLEAN_CAP_MB / FILE_CLEAN_ORPHAN_GRACE_MIN。
"""
import os, sys, sqlite3, datetime, time

if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')
import config_store as _cfg

DB_PATH   = '/opt/frontend/memories.db'
FILES_DIR = '/opt/frontend/static/uploads/files'
URL_PREFIX = '/static/uploads/files/'
LOG_FILE  = '/var/log/file_cleaner.log'


def _log(msg):
    ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    line = '[%s] %s\n' % (ts, msg)
    sys.stdout.write(line)
    try:
        open(LOG_FILE, 'a').write(line)
    except Exception:
        pass


def _db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _referenced_names(conn):
    """当前被 chat_messages 引用的文件名集合（去目录）。"""
    rows = conn.execute(
        "SELECT file_url FROM chat_messages WHERE file_url LIKE ?",
        (URL_PREFIX + '%',)
    ).fetchall()
    return set(os.path.basename(r['file_url']) for r in rows if r['file_url'])


def clean_orphans(conn, grace_min):
    if not os.path.isdir(FILES_DIR):
        return 0
    referenced = _referenced_names(conn)
    now = time.time()
    removed = 0
    for name in os.listdir(FILES_DIR):
        path = os.path.join(FILES_DIR, name)
        if not os.path.isfile(path):
            continue
        if name in referenced:
            continue
        # 宽限：最近 grace_min 分钟内刚落盘的文件先不碰（可能是在飞的上传）
        try:
            if now - os.path.getmtime(path) < grace_min * 60:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            pass
    if removed:
        _log('orphans removed: %d' % removed)
    return removed


def _dir_size(paths):
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def enforce_cap(conn, cap_mb):
    if not os.path.isdir(FILES_DIR) or cap_mb <= 0:
        return 0
    cap_bytes = cap_mb * 1024 * 1024
    files = []
    for name in os.listdir(FILES_DIR):
        path = os.path.join(FILES_DIR, name)
        if os.path.isfile(path):
            try:
                files.append((os.path.getmtime(path), name, path, os.path.getsize(path)))
            except OSError:
                pass
    total = sum(f[3] for f in files)
    if total <= cap_bytes:
        return 0
    files.sort()  # 最老在前
    removed = 0
    for _mt, name, path, size in files:
        if total <= cap_bytes:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        # 清空引用该文件的消息的 file 字段（消息保留）
        conn.execute(
            "UPDATE chat_messages SET file_url='', file_name='' WHERE file_url=?",
            (URL_PREFIX + name,))
        total -= size
        removed += 1
    if removed:
        conn.commit()
        _log('cap enforced (>%dMB): removed %d oldest files' % (cap_mb, removed))
    return removed


def run():
    grace_min = _cfg.get_int('FILE_CLEAN_ORPHAN_GRACE_MIN', 60)
    cap_mb    = _cfg.get_int('FILE_CLEAN_CAP_MB', 200)
    conn = _db()
    try:
        o = clean_orphans(conn, grace_min)
        c = enforce_cap(conn, cap_mb)
        if not o and not c:
            _log('nothing to clean')
    finally:
        conn.close()


if __name__ == '__main__':
    run()
