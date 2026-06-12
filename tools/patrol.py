#!/usr/bin/env python3
"""Server patrol script — reads logs, asks DeepSeek to analyse, stores result."""

import os, sys, sqlite3, subprocess, urllib.request, urllib.error, json
from datetime import datetime

DB_PATH  = '/opt/frontend/memories.db'
ENV_PATH = '/opt/frontend/.env'
API_URL  = 'https://api.deepseek.com/v1/chat/completions'
MODEL    = 'deepseek-v4-flash'

SYSTEM_PROMPT = (
    "你是一个服务器巡逻员，分析以下日志，找出错误、异常和需要关注的问题。"
    "用中文简洁描述，每个问题一行，格式：[级别] 描述。"
    "级别：❌严重 ⚠️警告 ℹ️信息。如果一切正常就回复：✅ 一切正常"
)

def load_key():
    for line in open(ENV_PATH):
        if line.startswith('DEEPSEEK_API_KEY='):
            return line.split('=', 1)[1].strip()
    raise RuntimeError("DEEPSEEK_API_KEY not found in .env")

def tail_file(path, n):
    try:
        r = subprocess.run(['tail', '-n', str(n), path],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or '(empty)'
    except Exception as e:
        return f'(error reading {path}: {e})'

def tail_journal(unit, n):
    try:
        r = subprocess.run(
            ['journalctl', '-u', unit, '-n', str(n),
             '--no-pager', '--output=short'],
            capture_output=True, text=True, timeout=6
        )
        lines = [l for l in r.stdout.splitlines() if not l.startswith('--')]
        return '\n'.join(lines).strip() or '(empty)'
    except Exception as e:
        return f'(error reading journal {unit}: {e})'

def ask_deepseek(api_key, user_content):
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
        data = json.loads(resp.read())
    return data['choices'][0]['message']['content'].strip()

def run_patrol():
    api_key = load_key()

    gw_log    = tail_journal('frontend-gw.service', 100)
    brain_log = tail_file('/var/log/ombre-brain.log', 100)
    nginx_log = tail_file('/var/log/nginx/error.log', 50)

    user_content = (
        "=== frontend-gw 日志（最近100行）===\n" + gw_log + "\n\n"
        "=== ombre-brain 日志（最近100行）===\n" + brain_log + "\n\n"
        "=== nginx error log（最近50行）===\n" + nginx_log
    )

    analysis = ask_deepseek(api_key, user_content)

    now    = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    report = f'[巡逻报告 {now}]\n{analysis}'

    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO fixes (content) VALUES (?)", (report,))

    if '❌' in analysis:
        critical = '\n'.join(l for l in analysis.splitlines() if '❌' in l)
        conn.execute("INSERT INTO bugs (content) VALUES (?)",
                     (f'[巡逻发现严重问题 {now}]\n{critical}',))

    conn.commit()
    conn.close()
    return analysis

if __name__ == '__main__':
    try:
        print(run_patrol())
    except Exception as e:
        print(f'[patrol error] {e}', file=sys.stderr)
        sys.exit(1)
