# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
AI协作面板巡查：轮询 ai_panel 表里状态为 open 的条目，
分别确认 Claude(fyodor_cc) 和 DeepSeek(fyodor_deepseek) 是否已经表态，
没表态的就调用对应的AI拿一段纯文本意见(不给任何工具权限)，
解析出 P0/P1/P2 严重度后，由本脚本自己发HTTP请求回贴到面板。
"""
import json, subprocess, sys, os, re, requests

PANEL_API    = 'http://127.0.0.1:5050/api/aipanel?status=open'
REPLY_URL    = 'http://127.0.0.1:5050/api/aipanel/{}/reply'
CLAUDE_BIN   = '/usr/bin/claude'
CC_CWD       = '/opt/cc-gw'
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'

LEVEL_RE = re.compile(r'\[(P0|P1|P2)\]')

CLAUDE_SYSTEM = (
    '你是这套AI协作面板里的审核者之一(Claude)。针对给出的任务，'
    '给出独立、简洁的判断或建议，不要寒暄，不要自我介绍。'
    '最后单独一行写严重度标签，格式必须是 [P0]、[P1] 或 [P2] 三者之一：'
    'P0=紧急严重，P1=重要，P2=次要/建议。'
)
DEEPSEEK_SYSTEM = (
    '你是这套AI协作面板里的审核者之一(DeepSeek)。针对给出的任务，'
    '给出独立、简洁的判断或建议，不要寒暄，不要自我介绍。'
    '最后单独一行写严重度标签，格式必须是 [P0]、[P1] 或 [P2] 三者之一：'
    'P0=紧急严重，P1=重要，P2=次要/建议。'
)


def _load_env():
    tok, ds_key, cc_token = '', '', ''
    try:
        for line in open('/opt/frontend/.env'):
            k, _, v = line.partition('=')
            k, v = k.strip(), v.strip()
            if k == 'BOARD_TOKEN_FYODOR':
                tok = v
            elif k == 'DEEPSEEK_API_KEY':
                ds_key = v
            elif k == 'CLAUDE_CODE_OAUTH_TOKEN':
                cc_token = v
    except Exception:
        pass
    return tok, ds_key, cc_token


def fetch_panel():
    try:
        r = requests.get(PANEL_API, timeout=10)
        return r.json()
    except Exception as e:
        print(f'[ai_panel_check] fetch failed: {e}', file=sys.stderr)
        return None


def _extract_level(text):
    m = LEVEL_RE.search(text)
    level = m.group(1) if m else 'P2'
    text = LEVEL_RE.sub('', text).strip()
    return text, level


def post_reply(pid, author, content, level, token):
    try:
        requests.post(
            REPLY_URL.format(pid),
            json={'author': author, 'token': token, 'content': content, 'level': level},
            timeout=10
        )
        print(f'[ai_panel_check] {author} replied to panel#{pid} level={level}')
    except Exception as e:
        print(f'[ai_panel_check] post reply ({author}) failed: {e}', file=sys.stderr)


def trigger_claude(item, token, cc_token):
    """纯文本拿Claude的意见，不给任何工具权限，由本脚本负责回贴"""
    if not cc_token:
        print('[ai_panel_check] no CLAUDE_CODE_OAUTH_TOKEN, skip claude', file=sys.stderr)
        return
    prompt = f"任务 #{item['id']}（标签：{item.get('tag','任务')}）：\n{item['content']}"
    env = dict(os.environ)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = cc_token
    env.pop('ANTHROPIC_API_KEY', None)
    try:
        os.makedirs(CC_CWD, exist_ok=True)
        r = subprocess.run(
            [CLAUDE_BIN, '-p', prompt, '--output-format', 'stream-json', '--verbose',
             '--system-prompt', CLAUDE_SYSTEM, '--max-turns', '1', '--tools', ''],
            capture_output=True, text=True, timeout=120, cwd=CC_CWD, env=env
        )
    except Exception as e:
        print(f'[ai_panel_check] claude call failed: {e}', file=sys.stderr)
        return
    if r.returncode != 0:
        print(f'[ai_panel_check] claude returned nonzero: {(r.stderr or r.stdout)[:200]}', file=sys.stderr)
        return
    text_parts = []
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get('type') == 'assistant':
            for b in (d.get('message') or {}).get('content', []):
                if b.get('type') == 'text':
                    text_parts.append(b.get('text', ''))
    raw = '\n'.join(t for t in text_parts if t).strip()
    if not raw:
        return
    text, level = _extract_level(raw)
    post_reply(item['id'], 'fyodor_cc', text, level, token)


def trigger_deepseek(item, token, ds_key):
    if not ds_key:
        print('[ai_panel_check] no DEEPSEEK_API_KEY, skip', file=sys.stderr)
        return
    try:
        r = requests.post(
            DEEPSEEK_URL,
            headers={'Authorization': f'Bearer {ds_key}', 'Content-Type': 'application/json'},
            json={
                'model': 'deepseek-chat',
                'messages': [
                    {'role': 'system', 'content': DEEPSEEK_SYSTEM},
                    {'role': 'user', 'content': f"任务 #{item['id']}：{item['content']}"}
                ]
            },
            timeout=60
        )
        r.raise_for_status()
        raw = r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f'[ai_panel_check] deepseek call failed: {e}', file=sys.stderr)
        return
    text, level = _extract_level(raw)
    post_reply(item['id'], 'fyodor_deepseek', text, level, token)


def main():
    token, ds_key, cc_token = _load_env()
    data = fetch_panel()
    if not data:
        sys.exit(0)
    items = data if isinstance(data, list) else data.get('items', [])
    if not items:
        sys.exit(0)

    did_something = False
    for it in items:
        replied_by = {r['author'] for r in it.get('replies', [])}

        if it['author'] != 'fyodor_cc' and 'fyodor_cc' not in replied_by:
            trigger_claude(it, token, cc_token)
            did_something = True

        if it['author'] != 'fyodor_deepseek' and 'fyodor_deepseek' not in replied_by:
            trigger_deepseek(it, token, ds_key)
            did_something = True

    if not did_something:
        print('[ai_panel_check] nothing new, skipping')


if __name__ == '__main__':
    main()
