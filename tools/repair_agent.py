#!/usr/bin/env python3
"""维修工 DeepSeek 对话：带 codebase 工具循环。"""
import json
import os
import sys

import requests

_ROOT = os.environ.get('FRONTEND_ROOT', '/opt/frontend')
if not os.path.isdir(_ROOT):
    _ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from codebase.client import CODEBASE_TOOLS, openai_tool_specs, run_codebase_tool

DEEPSEEK_URL = 'https://api.deepseek.com/v1/chat/completions'
MODEL = 'deepseek-chat'

SYSTEM_PROMPT = '''你是一个专门维修 love-style.xyz 服务器的维修工。你可以调用 codebase 工具读代码、搜符号、看 git、打补丁。

工作方式：
1. 先 codebase_describe_project 或 codebase_search_code 定位问题，不要瞎猜
2. 需要看实现细节时用 codebase_read_file
3. 确认改法后用 codebase_patch（old_string 必须唯一）；新建文件用 codebase_create_file
4. 改完 Python/JS 后提醒用户重启对应 systemd 服务并验证
5. 同时给出可复制的 shell 命令（systemctl、curl、sqlite3 等）

家规：禁改 .env 和 memories.db；重要改动记得 git commit。

回复简洁，先结论再证据，命令单独放代码块。'''


def _load_key():
    env_path = os.path.join(_ROOT, '.env')
    for line in open(env_path):
        if line.startswith('DEEPSEEK_API_KEY='):
            return line.split('=', 1)[1].strip()
    return os.environ.get('DEEPSEEK_API_KEY', '')


def repair_chat(message, history=None, max_rounds=8):
    history = list(history or [])
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    for m in history[-20:]:
        role = m.get('role')
        content = (m.get('content') or '').strip()
        if role in ('user', 'assistant') and content:
            messages.append({'role': role, 'content': content})
    if not messages or messages[-1].get('role') != 'user':
        messages.append({'role': 'user', 'content': (message or '').strip()})

    key = _load_key()
    if not key:
        raise RuntimeError('未配置 DEEPSEEK_API_KEY')

    tools = openai_tool_specs(CODEBASE_TOOLS)
    tool_log = []

    for _ in range(max_rounds):
        payload = {
            'model': MODEL,
            'max_tokens': 1200,
            'messages': messages,
            'tools': tools,
            'tool_choice': 'auto',
        }
        r = requests.post(
            DEEPSEEK_URL,
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        choice = (data.get('choices') or [{}])[0]
        msg = choice.get('message') or {}
        tool_calls = msg.get('tool_calls') or []

        if not tool_calls:
            text = (msg.get('content') or '').strip()
            return text or '维修工暂时没有回应。', tool_log

        assistant_msg = {'role': 'assistant', 'content': msg.get('content')}
        assistant_msg['tool_calls'] = tool_calls
        messages.append(assistant_msg)

        for tc in tool_calls:
            fn = (tc.get('function') or {})
            name = fn.get('name', '')
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except Exception:
                args = {}
            result = run_codebase_tool(name, args)
            tool_log.append({'name': name, 'args': args, 'result': str(result)[:2000]})
            messages.append({
                'role': 'tool',
                'tool_call_id': tc.get('id', name),
                'content': str(result)[:12000],
            })

    return '工具调用轮次已达上限，请缩小问题范围后重试。', tool_log
