#!/usr/bin/env python3
"""Server patrol script — reads logs, asks DeepSeek to analyse, stores result."""

import sqlite3, subprocess, urllib.request, json
from datetime import datetime, timedelta

DB_PATH  = '/opt/frontend/memories.db'
ENV_PATH = '/opt/frontend/.env'
API_URL  = 'https://api.deepseek.com/v1/chat/completions'
MODEL    = 'deepseek-v4-flash'

import sys as _sys
if '/opt/frontend' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend')
from bot_config import PATROL_SERVICES as SERVICES, PATROL_SYSTEM_PROMPT as SYSTEM_PROMPT

def _load_key():
    for line in open(ENV_PATH):
        if line.startswith('DEEPSEEK_API_KEY='):
            return line.split('=', 1)[1].strip()
    raise RuntimeError("DEEPSEEK_API_KEY not found in .env")

def _tail_file(path, n):
    try:
        r = subprocess.run(['tail', '-n', str(n), path],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or '(empty)'
    except Exception as e:
        return f'(error reading {path}: {e})'

def _tail_journal(unit, n):
    try:
        r = subprocess.run(
            ['journalctl', '-u', unit, '-n', str(n), '--no-pager', '--output=short'],
            capture_output=True, text=True, timeout=6
        )
        lines = [l for l in r.stdout.splitlines() if not l.startswith('--')]
        return '\n'.join(lines).strip() or '(empty)'
    except Exception as e:
        return f'(error reading journal {unit}: {e})'

def _check_services():
    """直接用 systemctl is-active 检查每个服务——不依赖时间戳推断。"""
    results = {}
    for svc in SERVICES:
        try:
            r = subprocess.run(['systemctl', 'is-active', svc],
                               capture_output=True, text=True, timeout=3)
            results[svc] = r.stdout.strip()   # 'active' / 'inactive' / 'failed'
        except Exception as e:
            results[svc] = f'error({e})'
    return results

def _ask_deepseek(api_key, user_content):
    payload = json.dumps({
        'model': MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user',   'content': user_content},
        ],
        'max_tokens': 1024,
        'temperature': 0.2,
    }).encode()
    req = urllib.request.Request(
        API_URL, data=payload, method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {api_key}'}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())['choices'][0]['message']['content'].strip()

def run_patrol():
    api_key = _load_key()
    now     = (datetime.now() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')  # 标题用北京时间，跟created_at一致

    # ── 1. 服务健康检查（直接判断，不依赖 AI）──────────────
    svc_status  = _check_services()
    down_svcs   = [s for s, st in svc_status.items() if st != 'active']
    svc_summary = '服务状态：' + ' | '.join(f'{s}={st}' for s, st in svc_status.items())

    # ── 2. 日志收集 + DeepSeek 分析 ─────────────────────────
    user_content = (
        f"=== 服务运行状态（systemctl is-active）===\n{svc_summary}\n\n"
        "=== frontend-gw 日志（最近100行）===\n" + _tail_journal('frontend-gw.service', 100) + "\n\n"
        "=== ombre-brain 日志（最近100行）===\n" + _tail_file('/var/log/ombre-brain.log', 100) + "\n\n"
        "=== nginx error log（最近50行）===\n"   + _tail_file('/var/log/nginx/error.log', 50)
    )
    analysis = _ask_deepseek(api_key, user_content)
    report   = f'[巡逻报告 {now}]\n{svc_summary}\n{analysis}'

    conn = sqlite3.connect(DB_PATH)

    # ── 4. 服务宕机 → board 紧急 ────────────────────────────
    if down_svcs:
        msg = f'[巡逻 {now}] 服务宕机：{", ".join(down_svcs)}'
        conn.execute(
            "INSERT INTO board (author,tag,content,status) VALUES ('patrol','紧急',?,'open')",
            (msg,)
        )

    # ── 5. AI 发现 ❌ → board 紧急 ──────────────────────────
    if '❌' in analysis:
        critical = '\n'.join(l for l in analysis.splitlines() if '❌' in l)
        msg = f'[巡逻发现严重问题 {now}]\n{critical}'
        conn.execute(
            "INSERT INTO board (author,tag,content,status) VALUES ('patrol','紧急',?,'open')",
            (msg,)
        )

    # ── 6. AI 发现 ⚠️（无 ❌）→ board 需求 ──────────────────
    elif '⚠️' in analysis:
        warn = '\n'.join(l for l in analysis.splitlines() if '⚠️' in l)
        conn.execute(
            "INSERT INTO board (author,tag,content,status) VALUES ('patrol','需求',?,'open')",
            (f'[巡逻 {now}] {warn}',)
        )

    conn.commit()
    conn.close()
    return analysis

def run_code_review():
    """CC改完代码后调用：检查语法、接口、关键表结构，结果写留言板。"""
    api_key = _load_key()
    now = (datetime.now() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')

    checks = []

    # 1. Python语法
    import py_compile, glob, traceback as _tb
    py_files = glob.glob('/opt/frontend/*.py') + glob.glob('/opt/frontend/tools/*.py')
    syntax_errors = []
    for f in py_files:
        try:
            py_compile.compile(f, doraise=True)
        except py_compile.PyCompileError as e:
            syntax_errors.append(f'{f}: {e}')
    checks.append('Python语法: ' + ('✅ 全部通过' if not syntax_errors else '❌ ' + '; '.join(syntax_errors)))

    # 2. 关键接口联通
    import urllib.request as _ur
    endpoints = [
        ('http://localhost:5050/api/todos', 'app'),
        ('http://localhost:5050/api/wishlist', 'wishlist'),
        ('http://localhost:5050/api/self_triggers/pending', 'self_trigger'),
        ('http://localhost:5050/api/ledger/trend', 'ledger_trend'),
    ]
    ep_results = []
    for url, name in endpoints:
        try:
            with _ur.urlopen(url, timeout=5) as r:
                ep_results.append(f'{name}={r.status}')
        except Exception as e:
            ep_results.append(f'{name}=❌{e}')
    checks.append('接口检查: ' + ' | '.join(ep_results))

    # 3. 数据库关键表存在
    conn = sqlite3.connect(DB_PATH)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    conn.close()
    required = ['chat_messages','dream_events','wake_log','self_triggers','board','wishlist','todos']
    missing = [t for t in required if t not in tables]
    checks.append('数据库表: ' + ('✅ 全部存在' if not missing else '❌ 缺失: ' + ','.join(missing)))

    # 4. 服务状态
    svc_status = _check_services()
    down = [s for s,st in svc_status.items() if st != 'active']
    checks.append('服务状态: ' + ('✅ 全部运行' if not down else '❌ 宕机: ' + ','.join(down)))

    summary = '\n'.join(checks)
    has_error = '❌' in summary
    report = f'[保洁阿姨验收报告 {now}]\n{summary}'

    conn2 = sqlite3.connect(DB_PATH)
    tag = '紧急' if has_error else '需求'
    status_str = 'open' if has_error else 'done'
    conn2.execute(
        "INSERT INTO board (author,tag,content,status,mentions) VALUES ('patrol',?,?,?,?)",
        (tag, report, status_str, 'fyodor_web' if has_error else '')
    )
    conn2.commit()
    conn2.close()
    print(report)
    return not has_error  # True=通过


if __name__ == '__main__':
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else 'patrol'
    try:
        if mode == 'code_review':
            ok = run_code_review()
            sys.exit(0 if ok else 1)
        else:
            result = run_patrol()
            print(result)
            if result and ('❌' in result or '⚠️' in result):
                subprocess.run(
                    [sys.executable, '/opt/frontend/tools/cc_board_check.py'],
                    timeout=360, check=False
                )
    except Exception as e:
        print(f'[patrol error] {e}', file=sys.stderr)
        sys.exit(1)
