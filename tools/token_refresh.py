#!/usr/bin/env python3
"""
自动同步 CLAUDE_CODE_OAUTH_TOKEN
每小时由 cron 运行一次。
如果 credentials.json 里的 token 更新了，或者快过期（<6h），
就自动写入 .env 并重启 frontend-gw。

gateway.py 用 CLAUDE_CODE_OAUTH_TOKEN 环境变量强制指定 token 给 claude CLI，
这个模式下 CLI 不会用 credentials.json 里的 refreshToken 自动刷新——
所以这里在快过期/已过期时，主动跑一次不带该环境变量覆盖的 claude 调用，
让 CLI 走它自己的 OAuth 刷新流程，再回读 credentials.json 拿到新 token。
"""
import json, re, os, subprocess, time, sys
from datetime import datetime

CREDS = '/root/.claude/.credentials.json'
ENV   = '/opt/frontend/.env'
SVC   = 'frontend-gw'

def log(msg):
    print(f"[token_refresh {datetime.now().strftime('%H:%M:%S')}] {msg}")

def read_creds():
    creds = json.load(open(CREDS))
    oauth = creds.get('claudeAiOauth', {})
    return oauth.get('accessToken', ''), oauth.get('expiresAt', 0)

try:
    new_token, expires_at = read_creds()
except Exception as e:
    log(f"读取 credentials 失败: {e}")
    sys.exit(1)

if not new_token:
    log("credentials 里没有 accessToken，跳过")
    sys.exit(0)

# 检查是否快过期（<6h），已过期也算
now_ms = int(time.time() * 1000)
hours_left = (expires_at - now_ms) / 3600000
if hours_left < 6:
    log(f"token 剩余 {hours_left:.1f}h，主动触发 CLI 刷新")
    env = dict(os.environ)
    env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)  # 让 CLI 用自己的 credentials.json + refreshToken
    try:
        r = subprocess.run(
            ['claude', '-p', 'reply with just: ok', '--tools', ''],
            env=env, capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            log(f"刷新调用失败（rc={r.returncode}）: {r.stderr.strip()[:200]}")
    except Exception as e:
        log(f"刷新调用异常: {e}")
    try:
        new_token, expires_at = read_creds()
        hours_left = (expires_at - int(time.time() * 1000)) / 3600000
        log(f"刷新后剩余 {hours_left:.1f}h")
    except Exception as e:
        log(f"刷新后重读 credentials 失败: {e}")

if hours_left < 0:
    log(f"token 仍处于过期状态（{abs(hours_left):.1f}h 前），需要手动 `claude login` 重新登录")
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
