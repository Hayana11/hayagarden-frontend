#!/usr/bin/env python3.11
"""
日常仪式触发器 — 每天凌晨3点由 cron 调用。
检查今天是否为特殊日期（冬至、生日），若是则触发对应的仪式。
"""
import sys, datetime, json, urllib.request

GW_WAKE = 'http://localhost:5051/wake'
LOG_FILE = '/var/log/daily_rituals.log'

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

def run():
    now = _now()
    month_day = now.strftime('%m-%d')
    
    ritual = None
    if month_day == '12-21':
        ritual = 'solstice'
    elif month_day == '04-22':
        ritual = 'birthday'
    
    if not ritual:
        _log(f"no ritual today ({month_day})")
        return
    
    _log(f"ritual triggered: {ritual}")
    try:
        payload = json.dumps({'mode': 'ritual', 'ritual_type': ritual}).encode()
        req = urllib.request.Request(
            GW_WAKE,
            data=payload,
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
        _log(f"ritual result: action={result.get('action')} content={str(result.get('content',''))[:80]}")
    except Exception as e:
        _log(f"ritual error: {e}")

if __name__ == '__main__':
    run()
