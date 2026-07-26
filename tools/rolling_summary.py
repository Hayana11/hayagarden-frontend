#!/usr/bin/env python3.11
"""滚动对话摘要——每 15 分钟跑（cron）。

边界与 build_messages 对齐：摘要覆盖所有被 conversation-content 裁掉的消息。
按 history mode 分桶存储，避免 legacy / relay / CC 互相污染。
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


def _sample(rows, cap=60):
    if len(rows) <= cap:
        return list(rows)
    head = list(rows[:15])
    tail = list(rows[-25:])
    mid_pool = list(rows[15:-25])
    step = max(1, len(mid_pool) // (cap - 40))
    mid = mid_pool[::step][:cap - 40]
    return head + mid + tail


def _enabled_modes():
    from chat.context_lean import lean_history_enabled
    if not lean_history_enabled():
        return ['legacy_block']
    return ['legacy_block', 'relay_hysteresis', 'cc_token_budget']


def _summarize_rows(rows, *, maxchar):
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
    return _ask(prompt)[:maxchar + 100]


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


def _run_mode(mode: str, *, horizon: int, maxchar: int, static_dir: str):
    from chat.history_boundary import boundary_rows_for_summary
    from chat.rolling_summary_store import clear_summary, get_summary, save_summary

    for_cc = mode == 'cc_token_budget'
    if mode == 'legacy_block':
        for_cc = False

    def get_db():
        return _db()

    trimmed_up_to_id, oldest_retained_id, rows = boundary_rows_for_summary(
        get_db,
        horizon_days=horizon,
        for_cc=for_cc,
        static_dir=static_dir,
    )
    if trimmed_up_to_id <= 0 or not rows:
        clear_summary(mode)
        _log('mode=%s no cropped messages (trimmed_up_to=%d oldest=%d), cleared' % (
            mode, trimmed_up_to_id, oldest_retained_id,
        ))
        return

    up_to = rows[-1]['id']
    current = get_summary(mode)
    if current.get('summary') and int(current.get('up_to_id') or 0) == up_to:
        _log('mode=%s unchanged cropped boundary (up_to id=%d), skip' % (mode, up_to))
        return

    try:
        summary = _summarize_rows(rows, maxchar=maxchar)
    except Exception as e:
        _log('mode=%s deepseek error: %s' % (mode, e))
        return
    if not summary:
        _log('mode=%s empty summary, skip' % mode)
        return

    save_summary(
        mode,
        summary=summary,
        up_to_id=up_to,
        oldest_retained_id=oldest_retained_id,
        msg_count=len(rows),
    )
    _log('mode=%s summarized %d pre-window msgs (up_to id=%d trimmed_up_to=%d): %s' % (
        mode, len(rows), up_to, trimmed_up_to_id, summary[:60],
    ))


def run(for_cc: bool = False):
    from chat.rolling_summary_store import ensure_tables

    horizon = _cfg.get_int('ROLLING_HORIZON_DAYS', 3)
    maxchar = _cfg.get_int('ROLLING_MAX_CHARS', 700)
    static_dir = os.path.join('/opt/frontend', 'static')
    conn = _db()
    ensure_tables(conn)
    conn.close()

    if for_cc:
        modes = ['cc_token_budget']
    else:
        modes = _enabled_modes()
    for mode in modes:
        _run_mode(mode, horizon=horizon, maxchar=maxchar, static_dir=static_dir)


if __name__ == '__main__':
    run()
