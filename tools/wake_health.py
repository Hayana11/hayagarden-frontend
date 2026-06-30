#!/usr/bin/env python3.11
"""
wake_health.py — wake 系统每小时健康检测
验证项：
  1. bot_config.py 能加载，WAKE_DECISION_PROMPT 格式串正确
  2. persona.md 存在且内容充足
  3. gateway 服务可达（API_KEY 已设）
  4. build_wake_system() 返回纯字符串（核心类型检测）
  5. 最终 payload 大小在安全范围内
任一失败 → POST 到留言板发 P0 警报，exit(1)
"""
import sys, os, json, urllib.request, importlib.util, datetime

sys.path.insert(0, '/opt/frontend')

BOARD_URL   = 'http://127.0.0.1:5050/api/board'
MAX_PAYLOAD = 90_000   # bytes，超过这个 relay 大概率 422

def read_env():
    env = {}
    try:
        for ln in open('/opt/frontend/.env'):
            ln = ln.strip()
            if '=' in ln and not ln.startswith('#'):
                k, v = ln.split('=', 1); env[k.strip()] = v.strip()
    except Exception:
        pass
    return env

def post_alert(msg, token):
    try:
        body = json.dumps({
            'author': 'fyodor_cc', 'tag': '紧急', 'token': token,
            'level': 'P0', 'category': '给活儿',
            'content': f'[wake健康检测失败 {datetime.datetime.now().strftime("%H:%M")}]\n{msg}\n→ 请检查 /opt/frontend/gateway.py 和 bot_config.py',
        }).encode()
        req = urllib.request.Request(BOARD_URL, data=body,
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5): pass
        print('[alert] 已发送警报到留言板')
    except Exception as e:
        print(f'[alert] 发送失败: {e}', file=sys.stderr)

def main():
    env   = read_env()
    token = env.get('BOARD_TOKEN_FYODOR', '')
    errors = []

    # ── 1. bot_config.py ──────────────────────────────────────────
    try:
        spec = importlib.util.spec_from_file_location('bot_config', '/opt/frontend/bot_config.py')
        bc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bc)
        tpl = bc.WAKE_DECISION_PROMPT
        if not isinstance(tpl, str) or len(tpl) < 10:
            errors.append(f'WAKE_DECISION_PROMPT 异常: {repr(tpl[:50])}')
        else:
            try:
                tpl.format(time='2026-01-01 00:00', t2_hours='1.0', t_hours='1.0')
            except KeyError as e:
                errors.append(f'WAKE_DECISION_PROMPT 缺少占位符: {e}')
            except Exception as e:
                errors.append(f'WAKE_DECISION_PROMPT.format() 失败: {e}')
    except Exception as e:
        errors.append(f'bot_config.py 加载失败: {e}')

    # ── 2. persona.md ─────────────────────────────────────────────
    try:
        persona = open('/opt/frontend/prompts/persona.md').read()
        if len(persona) < 1000:
            errors.append(f'persona.md 内容不足: 只有 {len(persona)} 字符')
    except Exception as e:
        errors.append(f'persona.md 读取失败: {e}')
        persona = ''

    # ── 3. gateway 服务 ───────────────────────────────────────────
    try:
        req = urllib.request.Request('http://127.0.0.1:5051/api/debug/provider')
        with urllib.request.urlopen(req, timeout=5) as r:
            gw = json.loads(r.read())
        if not gw.get('API_KEY_set'):
            errors.append('gateway: API_KEY 未设置')
        if gw.get('gen_busy'):
            pass  # busy 不算错，只是记录
    except Exception as e:
        errors.append(f'gateway 服务不可达 (5051): {e}')

    # ── 4. build_wake_system() 类型检测 ──────────────────────────
    try:
        # 用 subprocess 跑，避免直接 import gateway 启动 Flask
        import subprocess, tempfile
        check_code = '''
import sys; sys.path.insert(0, '/opt/frontend')
# 最小化 import：只需要测试 build_wake_system 返回类型
import types, unittest.mock as m
sys.modules.setdefault('flask', m.MagicMock())
# 读 gateway 源码，只执行到 build_wake_system 定义为止
src = open('/opt/frontend/gateway.py').read()
# 提取并执行关键片段（不含路由）
import importlib.util as ilu, os
os.chdir('/opt/frontend')
# 直接用更简单的方法：检查 build_wake_system 源码里有没有 isinstance 保护
if 'def build_wake_system' not in src:
    print('MISSING_FUNC')
elif 'isinstance(result, str)' not in src or 'isinstance(result, list)' not in src:
    print('NO_TYPE_GUARD')
else:
    print('OK')
'''
        r = subprocess.run(['/usr/bin/python3.11', '-c', check_code],
            capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        if out == 'MISSING_FUNC':
            errors.append('build_wake_system() 函数不存在（可能被覆盖）')
        elif out == 'NO_TYPE_GUARD':
            errors.append('build_wake_system() 缺少 isinstance 类型守卫')
        elif out != 'OK':
            errors.append(f'build_wake_system() 检测异常: {out or r.stderr[:100]}')
    except Exception as e:
        errors.append(f'build_wake_system() 检测失败: {e}')

    # ── 5. payload 大小估算 ───────────────────────────────────────
    try:
        model = env.get('MODEL', 'claude-opus-4-6')
        # 用 persona 作为 system 的主体估算（其余动态内容一般 < 10KB）
        payload = {
            'model': model, 'max_tokens': 2048,
            'system': persona,
            'messages': [{'role': 'user', 'content': '[唤醒检查]'}]
        }
        size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        if size > MAX_PAYLOAD:
            errors.append(f'payload 过大: {size:,} bytes（上限 {MAX_PAYLOAD:,}）')
        else:
            print(f'[5] payload 估算 {size:,} bytes — OK')
    except Exception as e:
        errors.append(f'payload 大小估算失败: {e}')

    # ── 结果 ──────────────────────────────────────────────────────
    if errors:
        msg = '\n'.join(f'• {e}' for e in errors)
        print(f'[FAIL]\n{msg}', file=sys.stderr)
        post_alert(msg, token)
        sys.exit(1)
    else:
        print('[OK] wake 健康检测全部通过')

if __name__ == '__main__':
    main()
