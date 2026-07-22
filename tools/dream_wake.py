#!/usr/bin/env python3.11
"""
AI 自主唤醒调度器 — 每30分钟由 cron 调用。
普通模式：用概率决定是否触发 /wake，取代原来"沉默>6h硬发消息"的 checkin 逻辑。
固定早安：每天 8:50 北京时间由 cron 调用 `dream_wake.py morning`（走 Wake 统一路径）。
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
    NIGHTWATCH_START, NIGHTWATCH_END, NIGHTWATCH_PROB, NIGHTWATCH_ACTIVITY_WINDOW,
)
# 唤醒时段/概率：以前是 bot_config.py 里的静态常量，只能靠 edit_bot_config
# 改源码字符串来调；现在改成从 config_store 读，费佳可以用 set_wake_settings
# 工具直接调，改了下次这个脚本被 cron 拉起时立即生效（本来就是每次全新进程）。
import config_store as _wcfg
WAKE_ACTIVE_START = _wcfg.get_int('WAKE_ACTIVE_START', 6)
WAKE_ACTIVE_END   = _wcfg.get_int('WAKE_ACTIVE_END', 3)
WAKE_PROB_MAX     = _wcfg.get_float('WAKE_PROB_MAX', 0.8)
WAKE_PROB_SCALE   = _wcfg.get_float('WAKE_PROB_SCALE', 2)
DREAMING_START = 3   # 凌晨3点后才做梦
DREAMING_END   = 5

def _now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

def _log(msg):
    ts = _now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    sys.stdout.write(line)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as log_file:
            log_file.write(line)
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
    """Shared interaction clock — effective idle hours, or None if unreliable."""
    from chat.interaction_state import read_interaction_clock
    clock = read_interaction_clock(_db, now=now)
    if not clock.reliable or clock.effective_idle_hours is None:
        return None
    return float(clock.effective_idle_hours)

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

        # 第四批：整夜同一次掷骰（按日期种子）。概率走 config_store，默认 0=不跳过；
        # 观测样本够了再 set dream_skip_prob=0.2，无需发版。
        skip_prob = _wcfg.get_float('dream_skip_prob', 0.0)
        if skip_prob > 0 and random.Random(f"dream-{now:%Y-%m-%d}").random() < skip_prob:
            _log(f"dream: probabilistic skip (p={skip_prob:.2f}, no dream tonight)")
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
        _log("nightwatch: no recent activity, skip")
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


def _morning_already_ran(wake_run_id: str) -> bool:
    """Return whether this exact fixed-morning run id is already recorded."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT 1 FROM wake_log WHERE wake_run_id=? LIMIT 1",
            (wake_run_id,),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def run_morning(now):
    """Fixed morning: gate locally, then use the unified /wake path."""
    wake_run_id = f"morning-{now.strftime('%Y-%m-%d')}"
    if _morning_already_ran(wake_run_id):
        _log(f"morning: duplicate run id {wake_run_id}, skip")
        return

    t_hours = _calc_t_hours(now)
    if t_hours is None:
        _log("morning: clock_unreliable, skip")
        return

    min_idle_min = float(_wcfg.get_float('WAKE_MIN_IDLE_MINUTES', 30) or 30)
    if t_hours < (min_idle_min / 60.0):
        _log(
            f"morning: recent_interaction T={t_hours:.3f}h "
            f"< {min_idle_min:.0f}m, skip"
        )
        return

    _log("morning triggered → calling /wake")
    try:
        result = _call_wake({'mode': 'morning', 'wake_run_id': wake_run_id})
        if result.get('skipped'):
            _log(f"morning skipped: {result.get('reason')}")
            return
        action = result.get('action', '?')
        thoughts = (result.get('thoughts') or '').strip()
        if thoughts:
            _log(f"morning result: {action} | {thoughts[:200]}")
        else:
            _log(f"morning result: {action}")
    except Exception as e:
        _log(f"morning error: {e}")


def _release_self_triggers(ids):
    """Restore claimed triggers to pending when /wake cannot run yet."""
    clean = [int(i) for i in ids if i is not None]
    if not clean:
        return
    try:
        payload = json.dumps({'ids': clean}).encode()
        req = urllib.request.Request(
            'http://localhost:5050/api/self_triggers/release',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        _log(f"self_trigger released ids={clean} → {body.get('released')}")
    except Exception as e:
        _log(f"self_trigger release error ids={clean}: {e}")


def run_self_triggers():
    """检查并触发到期的self_trigger——优先级最高，不受时段/概率限制。

    用原子 claim 接口（一步 UPDATE consumed=1 RETURNING）领取到期触发器，
    再以 mode=self_trigger 调 /wake（不受普通 30 分钟 idle 门槛）。
    若因 chat_generating / wake_in_progress 跳过，必须 release 回 pending，
    不得 claim 后静默丢失。
    """
    try:
        req = urllib.request.Request(
            'http://localhost:5050/api/self_triggers/claim',
            data=b'{}',
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            triggers = json.loads(r.read())
    except Exception as e:
        _log(f"self_trigger claim error: {e}")
        return False

    if not triggers:
        return False

    for t in triggers:
        tid = t['id']
        note = t.get('note') or ''
        _log(f"self_trigger #{tid} fired: {note[:40]}")
        try:
            result = _call_wake({
                'mode': 'self_trigger',
                'self_trigger_id': tid,
                'self_trigger_note': note,
            })
            if result.get('skipped'):
                reason = result.get('reason') or 'skipped'
                _log(f"self_trigger #{tid} skipped: {reason}")
                if reason in ('chat_generating', 'wake_in_progress'):
                    _release_self_triggers([tid])
                continue
            _log(f"self_trigger result: {result.get('action')}")
        except Exception as e:
            _log(f"self_trigger wake error: {e}")
            _release_self_triggers([tid])

    return True  # 本轮已处理self_trigger，跳过普通唤醒


def run():
    now = _now()

    # ── 最优先：自定义触发器 ─────────────────────────────────
    if run_self_triggers():
        return  # self_trigger处理完就结束本轮，不再走概率唤醒

    # 做梦：凌晨3-5点，她睡着60分钟后才触发
    if DREAMING_START <= now.hour < DREAMING_END:
        try:
            run_dreaming(now)
        except Exception:
            pass

    # 凌晨夜巡（1-3点）
    if _in_nightwatch_hours(now):
        run_nightwatch(now)
        return

    # 普通唤醒
    if not _in_active_hours(now):
        _log(f"skip: outside active hours")
        return

    t_hours = _calc_t_hours(now)
    if t_hours is None:
        _log("skip: clock_unreliable (fail closed)")
        return

    min_idle_min = float(_wcfg.get_float('WAKE_MIN_IDLE_MINUTES', 30) or 30)
    if t_hours < (min_idle_min / 60.0):
        _log(f"skip: recent_interaction T={t_hours:.3f}h < {min_idle_min:.0f}m")
        return

    p = min(WAKE_PROB_MAX, t_hours / WAKE_PROB_SCALE)
    roll = random.random()
    _log(f"T={t_hours:.1f}h p={p:.2f} roll={roll:.2f}")

    if roll >= p:
        _log("not triggered")
        return

    _log("triggered → calling /wake")
    try:
        result = _call_wake({})
        if result.get('skipped'):
            _log(f"wake skipped: {result.get('reason')}")
            return
        _act = result.get('action', '?')
        _th = (result.get('thoughts') or '').strip()
        if _th:
            _log(f"wake result: {_act} | {_th[:200]}")
        else:
            _log(f"wake result: {_act} | (no thoughts — check wake_log)")
    except Exception as e:
        _log(f"wake error: {e}")

if __name__ == '__main__':
    # selftrig 模式：只跑自定义触发器，供每分钟 cron 高频调用，让闹钟到点即触发
    # （不跑做梦/夜巡/概率唤醒那些重逻辑）。其余按原来的 30 分钟 run()。
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'selftrig':
        run_self_triggers()
    elif cmd == 'morning':
        run_morning(_now())
    else:
        run()
