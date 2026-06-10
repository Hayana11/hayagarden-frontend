#!/usr/bin/env python3
"""
用法：
  python3 push_tool.py morning   # 早安消息（每天 8:50 cron）
  python3 push_tool.py checkin   # 沉默超6小时主动消息（每小时 cron）
"""
import sys, sqlite3, datetime, json, urllib.request

DB_PATH  = '/opt/frontend/memories.db'
GW_PUSH  = 'http://localhost:5051/push'
LOG_FILE = '/var/log/push_tool.log'

def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def _now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

def _log(msg):
    ts = _now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    sys.stdout.write(line)
    try:
        open(LOG_FILE, 'a').write(line)
    except Exception:
        pass

def _call(prompt_type):
    payload = json.dumps({'prompt_type': prompt_type}).encode()
    req = urllib.request.Request(GW_PUSH, data=payload,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())

def morning():
    now   = _now()
    today = now.strftime('%Y-%m-%d')
    conn  = _db()
    exists = conn.execute(
        "SELECT id FROM chat_messages WHERE author IN ('fyodor','assistant') "
        "AND created_at BETWEEN ? AND ? LIMIT 1",
        (today + ' 00:00:00', today + ' 10:00:00')
    ).fetchone()
    conn.close()
    if exists:
        _log("morning: already sent today, skip"); return
    result = _call('morning')
    _log(f"morning: {result}")

def checkin():
    now    = _now()
    cutoff = (now - datetime.timedelta(hours=6)).strftime('%Y-%m-%d %H:%M:%S')
    conn   = _db()
    last_u = conn.execute(
        "SELECT created_at FROM chat_messages "
        "WHERE author NOT IN ('fyodor','assistant','claude') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_f = conn.execute(
        "SELECT created_at FROM chat_messages "
        "WHERE author IN ('fyodor','assistant','claude') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not last_u:
        _log("checkin: no user messages, skip"); return
    if last_u['created_at'] > cutoff:
        _log(f"checkin: hayana active {last_u['created_at']}, skip"); return
    if last_f and last_f['created_at'] > cutoff:
        _log(f"checkin: fyodor already sent {last_f['created_at']}, skip"); return
    result = _call('checkin')
    _log(f"checkin: {result}")

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'morning'
    {'morning': morning, 'checkin': checkin}.get(cmd, lambda: _log(f"unknown: {cmd}"))()
