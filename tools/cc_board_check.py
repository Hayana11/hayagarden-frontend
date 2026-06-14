#!/usr/bin/env python3
"""
零 token 预检脚本：定时轮询留言板，只在出现新的「紧急」或「需求」条目时才唤醒 CC。
状态文件保存最后处理过的最大 board ID，避免重复触发。
"""
import json, subprocess, sys, os
from urllib.request import urlopen
from urllib.error import URLError

BOARD_API   = 'http://localhost:5050/api/board?status=open'
STATE_FILE  = '/var/log/cc_board_seen_id'
CLAUDE_BIN  = '/usr/bin/claude'
TRIGGER_TAGS = {'紧急', '需求'}

def _load_board_token():
    try:
        for line in open('/opt/frontend/.env'):
            k, _, v = line.partition('=')
            if k.strip() == 'BOARD_TOKEN_FYODOR':
                return v.strip()
    except Exception:
        pass
    return ''

def load_seen_id():
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0

def save_seen_id(n):
    with open(STATE_FILE, 'w') as f:
        f.write(str(n))

def fetch_board():
    try:
        with urlopen(BOARD_API, timeout=10) as r:
            return json.loads(r.read())
    except URLError as e:
        print(f'[cc_board_check] fetch failed: {e}', file=sys.stderr)
        return None

def main():
    seen_id = load_seen_id()
    data = fetch_board()
    if data is None:
        sys.exit(0)

    items = data if isinstance(data, list) else data.get('items', [])
    if not items:
        sys.exit(0)

    max_id = max(it['id'] for it in items)

    new_trigger = [
        it for it in items
        if it['id'] > seen_id and it.get('tag') in TRIGGER_TAGS
    ]

    # Always advance seen_id to avoid re-triggering done items
    if max_id > seen_id:
        save_seen_id(max_id)

    if not new_trigger:
        sys.exit(0)

    # Build task description for CC
    lines = []
    for it in new_trigger:
        lines.append(f"#{it['id']} [{it['tag']}] {it['content'][:120]}")
    summary = '\n'.join(lines)

    board_token = _load_board_token()
    prompt = (
        f"board 上有 {len(new_trigger)} 条新的需要处理的条目：\n{summary}\n\n"
        "请逐条检查，能改代码就直接改并测试，完成后在对应条目下回复说明，"
        "并在回复里提到已处理完毕。如果暂时无法处理，也请在 board 上回复说明原因。\n\n"
        "回复留言板方法：POST http://127.0.0.1:5050/api/board/<id>/reply\n"
        f"JSON body: {{\"author\": \"fyodor\", \"token\": \"{board_token}\", \"content\": \"回复内容\"}}"
    )

    print(f'[cc_board_check] triggering CC for {len(new_trigger)} item(s): {[it["id"] for it in new_trigger]}')
    subprocess.run(
        [CLAUDE_BIN, '-p', prompt,
         '--allowedTools', 'mcp__home__exec_vps,mcp__ombre-brain__breath,mcp__ombre-brain__pulse,mcp__ombre-brain__grow,Bash,Read,Edit,Write,Glob,Grep',
         '--dangerously-skip-permissions'],
        timeout=300,
        check=False
    )

if __name__ == '__main__':
    main()
