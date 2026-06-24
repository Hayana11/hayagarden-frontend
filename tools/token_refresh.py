#!/usr/bin/env python3
"""
自动同步 CLAUDE_CODE_OAUTH_TOKEN
每小时由 cron 运行一次。
如果 credentials.json 里的 token 更新了，或者快过期（<6h），
就自动写入 .env 并重启 frontend-gw。
"""
import json, re, os, subprocess, time, sys
from datetime import datetime

CREDS = '/root/.claude/.credentials.json'
ENV   = '/opt/frontend/.env'
SVC   = 'frontend-gw'

def log(msg):
    print(f"[token_refresh {datetime.now().strftime('%H:%M:%S')}] {msg}")

try:
    creds = json.load(open(CREDS))
    oauth = creds.get('claudeAiOauth', {})
    new_token  = oauth.get('accessToken', '')
    expires_at = oauth.get('expiresAt', 0)  # ms timestamp
except Exception as e:
    log(f"读取 credentials 失败: {e}")
    sys.exit(1)

if not new_token:
    log("credentials 里没有 accessToken，跳过")
    sys.exit(0)

# 检查是否快过期（<6h）
now_ms = int(time.time() * 1000)
hours_left = (expires_at - now_ms) / 3600000
if hours_left < 0:
    log(f"token 已过期 {abs(hours_left):.1f}h 前，需要重新登录")
    sys.exit(1)

# 读取 .env 里现有 token
env_src = open(ENV).read()
m = re.search(r'CLAUDE_CODE_OAUTH_TOKEN=(.*)', env_src)
cur_token = m.group(1).strip() if m else ''

if cur_token == new_token:
    log(f"token 一致，剩余 {hours_left:.1f}h，无需更新")
    if hours_left < 6:
        log(f"警告：token 剩余不足 6h，请留意是否需要手动刷新")
    sys.exit(0)

# token 有变化才更新 .env 并重启
env_new = re.sub(
    r'CLAUDE_CODE_OAUTH_TOKEN=.*',
    f'CLAUDE_CODE_OAUTH_TOKEN={new_token}',
    env_src
)
open(ENV, 'w').write(env_new)
log(f"token 已更新，剩余 {hours_left:.1f}h")

# 重启服务
r = subprocess.run(['systemctl', 'restart', SVC], capture_output=True)
if r.returncode == 0:
    log(f"{SVC} 重启成功")
else:
    log(f"{SVC} 重启失败: {r.stderr.decode()[:100]}")
    sys.exit(1)
