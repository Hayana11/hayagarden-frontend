#!/usr/bin/env python3.11
"""
AI 自主唤醒调度器 — 每30分钟由 cron 调用。
普通模式：用概率决定是否触发 /wake，取代原来"沉默>6h硬发消息"的 checkin 逻辑。
夜巡模式：凌晨1-3点，如果 dream_events 显示她还醒着，以低概率静静出现。
梦境模式：当她睡着60+分钟，自动生成梦。
"""
import sys, sqlite3, datetime, json, urllib.request, random

DB_PATH  = '/opt/frontend/memories.db'
GW_WAKE  = 'http://localhost:5051/wake'
LOG_FILE = '/var/log/dream_wake.log'

if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')
from bot_config import (
    WAKE_ACTIVE_START, WAKE_ACTIVE_END, WAKE_PROB_MAX, WAKE_PROB_SCALE,
    NIGHTWATCH_START, NIGHTWATCH_END, NIGHTWATCH_PROB, NIGHTWATCH_ACTIVITY_WINDOW,
)

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
    """只在 08:00 - 次日 01:00 允许触发"""
    h = now.hour
    return h >= WAKE_ACTIVE_START or h < WAKE_ACTIVE_END

def _in_nightwatch_hours(now):
    """凌晨夜巡时段：NIGHTWATCH_START ~ NIGHTWATCH_END"""
    h = now.hour
    return NIGHTWATCH_START <= h < NIGHTWATCH_END

def _calc_t_hours(now):
    """计算距离"上次有效互动"的小时数"""
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

def _get_screen_off_minutes(now):
    """读取 /api/screen 写入的状态；若屏幕已关闭，返回关闭时长(分钟)，否则 None"""
    try:
        with open('/opt/frontend/screen_state.json') as f:
            state = json.load(f)
        if state.get('status') != 'off':
            return None
        t = datetime.datetime.fromisoformat(state['time'])
        t_utc_naive = t.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        now_utc_naive = now - datetime.timedelta(hours=8)  # now 是北京时间 naive
        return (now_utc_naive - t_utc_naive).total_seconds() / 60
    except Exception:
        return None

def _get_recent_activity(now):
    """查询 dream_events 最近的记录；屏幕已关闭一段时间则优先判定为不活跃，
    避免 dream_events 残留记录导致"已入睡却被判定还醒着"。"""
    off_min = _get_screen_off_minutes(now)
    if off_min is not None and off_min >= 10:
        return False, f"屏幕已关闭约{off_min:.0f}分钟，应已入睡"
    conn = _db()
    cutoff = now - datetime.timedelta(minutes=NIGHTWATCH_ACTIVITY_WINDOW)
    cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
    row = conn.execute(
        "SELECT value FROM dream_events WHERE created_at > ? ORDER BY id DESC LIMIT 1",
        (cutoff_str,)
    ).fetchone()
    conn.close()
    if row:
        return True, f"感知层显示她最近在{row['value']}"
    return False, "感知层无近期活动记录"

def _call_wake(payload: dict):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        GW_WAKE,
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())

def run_dreaming(now):
    """当她睡着60+分钟，自动生成梦"""
    conn = _db()
    last_event = conn.execute(
        "SELECT created_at FROM dream_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    
    if not last_event:
        return
    
    try:
        event_time = datetime.datetime.strptime(last_event['created_at'], '%Y-%m-%d %H:%M:%S')
        sleep_duration = (now - event_time).total_seconds() / 60
        
        if sleep_duration < 60:
            return
        
        conn2 = _db()
        recent_dream = conn2.execute(
            "SELECT id FROM posts WHERE type='DREAM' AND created_at > datetime('now', '+8 hours', '-2 hours')"
        ).fetchone()
        conn2.close()
        
        if recent_dream:
            _log(f"dream: already has recent dream in past 2 hours")
            return
        
        _log(f"dream: sleep {sleep_duration:.0f}min, generating")
        from dream_generator import generate_dream
        generate_dream()
    except Exception as e:
        _log(f"dream error: {e}")

def run_nightwatch(now):
    """凌晨她还醒着时低概率出现"""
    active, activity_desc = _get_recent_activity(now)
    if not active:
        _log(f"nightwatch: no recent activity, skip")
        return

    roll = random.random()
    _log(f"nightwatch p={NIGHTWATCH_PROB:.2f} roll={roll:.2f}")

    if roll >= NIGHTWATCH_PROB:
        _log("nightwatch: not triggered")
        return

    _log("nightwatch triggered")
    try:
        result = _call_wake({'mode': 'nightwatch', 'activity_desc': activity_desc})
        _log(f"nightwatch result: {result.get('action')}")
    except Exception as e:
        _log(f"nightwatch error: {e}")

def run():
    now = _now()

    # 做梦只在凌晨 1-3 点检查（NIGHTWATCH 时段），避免白天/傍晚被误判"睡着60分钟"而做梦
    if _in_nightwatch_hours(now):
        try:
            run_dreaming(now)
        except Exception:
            pass

    # 凌晨夜巡
    if _in_nightwatch_hours(now):
        run_nightwatch(now)
        return

    # 普通唤醒
    if not _in_active_hours(now):
        _log(f"skip: outside active hours")
        return

    t_hours = _calc_t_hours(now)
    p = min(WAKE_PROB_MAX, t_hours / WAKE_PROB_SCALE)
    roll = random.random()
    _log(f"T={t_hours:.1f}h p={p:.2f} roll={roll:.2f}")

    if roll >= p:
        _log("not triggered")
        return

    _log("triggered → calling /wake")
    try:
        result = _call_wake({})
        _log(f"wake result: {result.get('action')}")
    except Exception as e:
        _log(f"wake error: {e}")

if __name__ == '__main__':
    run()
