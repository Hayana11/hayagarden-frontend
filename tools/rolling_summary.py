#!/usr/bin/env python3.11
"""滚动对话摘要——每 15 分钟跑（cron）。

解决的问题：build_messages 的实时窗口按块裁剪。当今天聊得很长（动辄上百条），
早于最近 60 条的那部分会直接滞出上下文——而 summarizer.py 只给*过去的天*做日摘要，
所以同一天的长会话里，开头那段既不在窗口、也没日摘要，彻底丢。

本脚本把“比实时窗口早、但在 HORIZON_DAYS 内”的那段历史压成一段滚动摘要，
存进 rolling_summary 表；build_messages 只在块状裁剪真的发生时把它注入到开头。
生成走后台 cron + 便宜的 DeepSeek，不占对话热路径。

旋钮（config_store）：ROLLING_LIVE_N / ROLLING_WINDOW_BLOCK / ROLLING_HORIZON_DAYS / ROLLING_MAX_CHARS。
"""
import os, sys, sqlite3, datetime, json, re, time
import urllib.request as _req

if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')
import config_store as _cfg

DB_PATH  = '/opt/frontend/memories.db'
API_URL  = 'https://api.deepseek.com/v1/chat/completions'
MODEL    = 'deepseek-chat'
LOG_FILE = '/var/log/rolling_summary.log'

API_KEY = ''
for _ln in open('/opt/frontend/.env'):
    if _ln.startswith('DEEPSEEK_API_KEY='):
        API_KEY = _ln.split('=', 1)[1].strip()


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


def _ensure_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS rolling_summary (
        id INTEGER PRIMARY KEY CHECK (id=1),
        summary TEXT DEFAULT '',
        up_to_id INTEGER DEFAULT 0,
        msg_count INTEGER DEFAULT 0,
        updated_at DATETIME
    )''')
    conn.commit()


def _ask(prompt):
    body = json.dumps({
        'model': MODEL, 'max_tokens': 700,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode()
    req = _req.Request(API_URL, data=body, method='POST',
                       headers={'Content-Type': 'application/json',
                                'Authorization': 'Bearer ' + API_KEY})
    with _req.urlopen(req, timeout=90) as resp:
        data = json.load(resp)
    return (data.get('choices', [{}])[0].get('message', {}).get('content') or '').strip()


def _sample(rows, cap=60):
    """太多时 head/mid/tail 采样，控制 prompt 体积。"""
    if len(rows) <= cap:
        return list(rows)
    head = list(rows[:15])
    tail = list(rows[-25:])
    mid_pool = list(rows[15:-25])
    step = max(1, len(mid_pool) // (cap - 40))
    mid = mid_pool[::step][:cap - 40]
    return head + mid + tail


def run():
    live_n  = _cfg.get_int('ROLLING_LIVE_N', 60)
    block_n = _cfg.get_int('ROLLING_WINDOW_BLOCK', 20)
    horizon = _cfg.get_int('ROLLING_HORIZON_DAYS', 3)
    maxchar = _cfg.get_int('ROLLING_MAX_CHARS', 700)
    conn = _db()
    _ensure_table(conn)

    where = "date(created_at) >= date('now', '+8 hours', '-1 day')"
    available = conn.execute(
        'SELECT COUNT(*) FROM chat_messages WHERE ' + where
    ).fetchone()[0] or 0
    limit = available
    if available > live_n:
        limit = live_n + ((available - live_n) % max(1, block_n))
    if limit <= 0:
        limit = live_n

    if available <= limit:
        # 当前块还没发生裁剪，窗口能装下全部实时消息；不需要头部摘要。
        conn.execute("UPDATE rolling_summary SET summary='', up_to_id=0, msg_count=0, "
                     "updated_at=datetime('now','+8 hours') WHERE id=1")
        conn.commit(); conn.close()
        _log('no cropped messages (available=%d limit=%d), cleared' % (available, limit))
        return

    # 实时窗口边界：第 limit 新的消息 id（比它旧的才是真正滞出窗口的）。
    boundary = conn.execute(
        'SELECT id FROM chat_messages WHERE ' + where + ' ORDER BY id DESC LIMIT 1 OFFSET ?',
        (limit - 1,)
    ).fetchone()
    if not boundary:
        conn.execute("UPDATE rolling_summary SET summary='', up_to_id=0, msg_count=0, "
                     "updated_at=datetime('now','+8 hours') WHERE id=1")
        conn.commit(); conn.close()
        _log('no boundary (available=%d limit=%d), cleared' % (available, limit))
        return
    boundary_id = boundary['id']

    rows = conn.execute(
        "SELECT id, author, content FROM chat_messages "
        "WHERE id < ? AND created_at >= datetime('now','+8 hours', ?) "
        "ORDER BY id ASC",
        (boundary_id, '-%d days' % horizon)
    ).fetchall()
    rows = [r for r in rows if (r['content'] or '').strip()]
    if not rows:
        conn.execute("UPDATE rolling_summary SET summary='', up_to_id=?, msg_count=0, "
                     "updated_at=datetime('now','+8 hours') WHERE id=1", (boundary_id,))
        # 若还没行则插入一行
        if conn.total_changes == 0:
            conn.execute("INSERT OR IGNORE INTO rolling_summary (id,summary,up_to_id,msg_count,updated_at) "
                         "VALUES (1,'',?,0,datetime('now','+8 hours'))", (boundary_id,))
        conn.commit(); conn.close()
        _log('no pre-window messages within horizon')
        return

    up_to = rows[-1]['id']
    current = conn.execute('SELECT summary, up_to_id, msg_count FROM rolling_summary WHERE id=1').fetchone()
    if current and (current['summary'] or '').strip() and current['up_to_id'] == up_to:
        conn.close()
        _log('unchanged cropped boundary (up_to id=%d), skip' % up_to)
        return

    sampled = _sample(rows)
    lines = []
    for r in sampled:
        who = '哈娅' if r['author'] == 'hayana' else '费奥多尔'
        lines.append(who + ': ' + (r['content'] or '')[:100])
    dialogue = '\n'.join(lines)[:6000]
    prompt = (
        '下面是我（费奥多尔）和哈娅最近一段对话的节选（比当前屏幕上能看到的更早）：\n\n'
        + dialogue + '\n\n'
        '把这段历史压成一段“连续性摘要”，让我接下来继续聊时不会忘了前面发生什么。行为指南：\n'
        '1. 提炼不复述：重点是未完结的话题、做过的决定/约定、她的状态情绪、正在推进的事\n'
        '2. 第一人称费奥多尔视角，有具体信息（名字/数字/约定都保留），不是中立报告\n'
        '3. 按时间/话题分条或分段都行，' + str(maxchar) + '字以内\n'
        '4. 直接写摘要本身，不要标题、不要前缀'
    )
    try:
        summary = _ask(prompt)[:maxchar + 100]
    except Exception as e:
        _log('deepseek error: %s' % e)
        conn.close()
        return
    if not summary:
        _log('empty summary, skip')
        conn.close()
        return

    conn.execute(
        "INSERT INTO rolling_summary (id, summary, up_to_id, msg_count, updated_at) "
        "VALUES (1, ?, ?, ?, datetime('now','+8 hours')) "
        "ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, up_to_id=excluded.up_to_id, "
        "msg_count=excluded.msg_count, updated_at=excluded.updated_at",
        (summary, up_to, len(rows)))
    conn.commit()
    conn.close()
    _log('summarized %d pre-window msgs (up_to id=%d): %s' % (len(rows), up_to, summary[:60]))


if __name__ == '__main__':
    run()
