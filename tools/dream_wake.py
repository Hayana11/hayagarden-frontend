#!/usr/bin/env python3.11
"""
AI 自主唤醒调度器 — 每30分钟由 cron 调用。
用概率决定是否触发 /wake，取代原来"沉默>6h硬发消息"的 checkin 逻辑。
"""
import sys, sqlite3, datetime, json, urllib.request, random, math

DB_PATH  = '/opt/frontend/memories.db'
GW_WAKE  = 'http://localhost:5051/wake'
LOG_FILE = '/var/log/dream_wake.log'

import sys as _sys
if '/opt/frontend' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend')
from bot_config import WAKE_ACTIVE_START, WAKE_ACTIVE_END, WAKE_PROB_MAX, WAKE_PROB_SCALE

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

def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def _in_active_hours(now):
    """只在 08:00 - 次日 01:00 允许触发，避免凌晨骚扰。"""
    h = now.hour
    return h >= WAKE_ACTIVE_START or h < WAKE_ACTIVE_END

def _calc_t_hours(now):
    """计算距离"上次有效互动"的小时数。"""
    conn = _db()
    last_user = conn.execute(
        "SELECT created_at FROM chat_messages "
        "WHERE author NOT IN ('fyodor','assistant','claude') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_wake_msg = conn.execute(
        "SELECT woke_at FROM wake_log WHERE action='message' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    t = 999.0
    if last_user:
        try:
            lu_dt = datetime.datetime.strptime(last_user['created_at'], '%Y-%m-%d %H:%M:%S')
            t = min(t, (now - lu_dt).total_seconds() / 3600)
        except Exception:
            pass
    if last_wake_msg:
        try:
            lw_dt = datetime.datetime.strptime(last_wake_msg['woke_at'], '%Y-%m-%d %H:%M:%S')
            t = min(t, (now - lw_dt).total_seconds() / 3600)
        except Exception:
            pass
    return t

def run():
    now = _now()
    if not _in_active_hours(now):
        _log(f"skip: outside active hours ({now.hour}:00)")
        return

    t_hours = _calc_t_hours(now)
    p = min(WAKE_PROB_MAX, t_hours / WAKE_PROB_SCALE)
    roll = random.random()
    _log(f"T={t_hours:.1f}h p={p:.2f} roll={roll:.2f}")

    if roll >= p:
        _log("not triggered this round")
        return

    _log("triggered → calling /wake")
    try:
        req = urllib.request.Request(
            GW_WAKE,
            data=b'{}',
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
        _log(f"wake result: action={result.get('action')} content={str(result.get('content',''))[:60]}")
    except Exception as e:
        _log(f"wake error: {e}")

if __name__ == '__main__':
    run()
