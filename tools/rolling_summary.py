#!/usr/bin/env python3.11
"""滚动对话摘要——每 15 分钟跑（cron）。

边界与 build_messages 对齐：摘要覆盖所有被 conversation-content 裁掉的消息。
"""
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request as _req

if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')
import config_store as _cfg

DB_PATH = '/opt/frontend/memories.db'
API_URL = 'https://api.deepseek.com/v1/chat/completions'
MODEL = 'deepseek-chat'
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
    if len(rows) <= cap:
        return list(rows)
    head = list(rows[:15])
    tail = list(rows[-25:])
    mid_pool = list(rows[15:-25])
    step = max(1, len(mid_pool) // (cap - 40))
    mid = mid_pool[::step][:cap - 40]
    return head + mid + tail


def run(for_cc: bool = False):
    from chat.history_boundary import boundary_rows_for_summary

    horizon = _cfg.get_int('ROLLING_HORIZON_DAYS', 3)
    maxchar = _cfg.get_int('ROLLING_MAX_CHARS', 700)
    conn = _db()
    _ensure_table(conn)
    conn.close()

    def get_db():
        return _db()

    trimmed_up_to_id, oldest_retained_id, rows = boundary_rows_for_summary(
        get_db, horizon_days=horizon, for_cc=for_cc,
    )
    conn = _db()
    if trimmed_up_to_id <= 0 or not rows:
        conn.execute(
            "UPDATE rolling_summary SET summary='', up_to_id=0, msg_count=0, "
            "updated_at=datetime('now','+8 hours') WHERE id=1"
        )
        if conn.total_changes == 0:
            conn.execute(
                "INSERT OR IGNORE INTO rolling_summary (id,summary,up_to_id,msg_count,updated_at) "
                "VALUES (1,'',0,0,datetime('now','+8 hours'))"
            )
        conn.commit()
        conn.close()
        _log('no cropped messages (trimmed_up_to=%d oldest=%d), cleared' % (
            trimmed_up_to_id, oldest_retained_id,
        ))
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
        (summary, up_to, len(rows)),
    )
    conn.commit()
    conn.close()
    _log('summarized %d pre-window msgs (up_to id=%d trimmed_up_to=%d): %s' % (
        len(rows), up_to, trimmed_up_to_id, summary[:60],
    ))


if __name__ == '__main__':
    run()
