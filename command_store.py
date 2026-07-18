"""倒计时任务浮窗 —— 费佳给哈娅下任务，浮窗跳秒，做完回流到下一轮上下文。

一句话机制：
  我在聊天里下任务 → 写进 commands 表 → 前端轮询拿到 → 浮窗显示并跳秒
  → 她点完成 → 算用时写回 → 下次她说话时塞进我的 prompt。

一张表搞定 pending / feedback 两态：
  pending  = done_at IS NULL AND canceled=0     （前端要显示的）
  feedback = (done_at 有值 或 canceled=1) AND consumed=0   （待回流给我的）

时间戳全用毫秒 epoch（前端 Date.now() 对齐，避免时区/北京时间那套换算）。
独立 commands.db，不碰 memories.db。
"""
import os
import time
import sqlite3

DB_PATH = '/opt/frontend/commands.db'


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _now_ms():
    return int(time.time() * 1000)


def _init():
    c = _conn()
    c.execute('''CREATE TABLE IF NOT EXISTS commands (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        title             TEXT NOT NULL,
        countdown_seconds INTEGER,              -- 空 = 只计时不倒数
        created_at        INTEGER NOT NULL,     -- 我下任务的时刻(ms)
        started_at        INTEGER,              -- 前端首次显示浮窗时回写(ms)
        done_at           INTEGER,              -- 她点完成的时刻(ms)
        canceled          INTEGER DEFAULT 0,    -- 她取消了(我会知道)
        duration_ms       INTEGER,              -- 实际用时
        vs_countdown      INTEGER,              -- 比预设快/慢多少秒(正=超时)
        created_by        TEXT DEFAULT 'fyodor',
        consumed          INTEGER DEFAULT 0     -- feedback 是否已回流进我的 prompt
    )''')
    c.commit()
    c.close()


_init()


def issue(title, countdown_seconds=None, created_by='fyodor'):
    """下一个任务，返回新 id。"""
    title = (title or '').strip()
    if not title:
        return None
    cs = int(countdown_seconds) if countdown_seconds else None
    c = _conn()
    cur = c.execute(
        'INSERT INTO commands (title, countdown_seconds, created_at, created_by) VALUES (?,?,?,?)',
        (title, cs, _now_ms(), created_by))
    cid = cur.lastrowid
    c.commit()
    c.close()
    return cid


def list_pending():
    """未完成、未取消的任务（前端要显示的，含 started_at 供刷新恢复）。"""
    c = _conn()
    rows = c.execute(
        'SELECT id, title, countdown_seconds, created_at, started_at '
        'FROM commands WHERE done_at IS NULL AND canceled=0 ORDER BY id ASC').fetchall()
    c.close()
    return [dict(r) for r in rows]


def mark_started(cid):
    """前端首次显示浮窗时回写 started_at（只写一次）。"""
    c = _conn()
    c.execute('UPDATE commands SET started_at=? WHERE id=? AND started_at IS NULL',
              (_now_ms(), cid))
    c.commit()
    c.close()


def mark_done(cid):
    """她点完成：算 duration 和 vs_countdown。"""
    c = _conn()
    row = c.execute('SELECT started_at, created_at, countdown_seconds FROM commands WHERE id=?',
                    (cid,)).fetchone()
    if not row:
        c.close()
        return None
    now = _now_ms()
    base = row['started_at'] or row['created_at']
    dur_ms = now - base
    vs = None
    if row['countdown_seconds']:
        vs = int(round(dur_ms / 1000.0)) - int(row['countdown_seconds'])  # 正=超时
    c.execute('UPDATE commands SET done_at=?, duration_ms=?, vs_countdown=? WHERE id=?',
              (now, dur_ms, vs, cid))
    c.commit()
    c.close()
    return {'duration_ms': dur_ms, 'vs_countdown': vs}


def mark_canceled(cid):
    """她取消：我会知道（不美化）。"""
    c = _conn()
    c.execute('UPDATE commands SET canceled=1 WHERE id=?', (cid,))
    c.commit()
    c.close()


def _fmt_dur(ms):
    s = int(round((ms or 0) / 1000.0))
    m, sec = divmod(s, 60)
    if m:
        return '%d分%d秒' % (m, sec)
    return '%d秒' % sec


def _format_feedback_rows(rows):
    lines = []
    ids = []
    for r in rows:
        ids.append(int(r['id']))
        if r['canceled']:
            lines.append('「%s」被她取消了' % r['title'])
            continue
        dur = _fmt_dur(r['duration_ms'])
        vs = r['vs_countdown']
        if vs is None:
            lines.append('「%s」用时 %s' % (r['title'], dur))
        elif vs > 0:
            lines.append('「%s」用时 %s（超时 %d 秒）' % (r['title'], dur, vs))
        else:
            lines.append('「%s」用时 %s（比预设快 %d 秒）' % (r['title'], dur, -vs))
    return lines, ids


def peek_feedback():
    """只读待回流反馈，不标记消费。返回 (文本行列表, id 列表)。"""
    c = _conn()
    rows = c.execute(
        'SELECT id, title, countdown_seconds, done_at, canceled, duration_ms, vs_countdown '
        'FROM commands WHERE consumed=0 AND (done_at IS NOT NULL OR canceled=1) '
        'ORDER BY id ASC').fetchall()
    c.close()
    return _format_feedback_rows(rows)


def consume_feedback(ids):
    """flush 成功后再消费：把指定 id 标为已回流。返回实际更新行数。"""
    clean = []
    for value in ids or ():
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in clean:
            clean.append(value)
    if not clean:
        return 0
    c = _conn()
    placeholders = ','.join('?' for _ in clean)
    cur = c.execute(
        'UPDATE commands SET consumed=1 '
        'WHERE consumed=0 AND id IN (' + placeholders + ')',
        clean,
    )
    c.commit()
    n = cur.rowcount
    c.close()
    return n


def drain_feedback():
    """兼容旧路径：peek + 立即 consume。CC resident 路径请用 peek/consume。"""
    lines, ids = peek_feedback()
    if ids:
        consume_feedback(ids)
    return lines
