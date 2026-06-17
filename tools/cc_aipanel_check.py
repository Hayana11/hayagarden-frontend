#!/usr/bin/env python3
"""
触发 CC（蛋壳）处理 AI 协作面板上的特定任务。
Usage: python3.11 tools/cc_aipanel_check.py <panel_id>
"""
import sys, os, json, subprocess
from urllib.request import urlopen, Request

API_BASE   = 'http://127.0.0.1:5050'
CLAUDE_BIN = '/usr/bin/claude'
DS_SCRIPT  = '/opt/frontend/tools/deepseek_review.py'
PYTHON     = '/usr/bin/python3.11'


def _load_token():
    try:
        for line in open('/opt/frontend/.env'):
            k, _, v = line.partition('=')
            if k.strip() == 'BOARD_TOKEN_FYODOR':
                return v.strip()
    except Exception:
        pass
    return ''


def fetch_item(pid):
    url = f'{API_BASE}/api/aipanel'
    with urlopen(url, timeout=10) as r:
        items = json.loads(r.read())
    for it in items:
        if it['id'] == pid:
            return it
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: cc_aipanel_check.py <panel_id>", file=sys.stderr)
        sys.exit(1)
    pid   = int(sys.argv[1])
    token = _load_token()

    item = fetch_item(pid)
    if not item:
        print(f'[cc_aipanel] panel #{pid} not found', file=sys.stderr)
        sys.exit(1)

    title = item.get('title') or item['content'][:80]
    body  = item.get('body') or ''
    kind  = item.get('kind') or 'task'

    ctx_lines = [f'AI协作面板任务 #{pid} [{kind}]', f'标题：{title}']
    if body:
        ctx_lines += ['', '详情：', body]
    replies = item.get('replies') or []
    if replies:
        ctx_lines.append('')
        for r in replies:
            ctx_lines.append(f"{r['author']}（{r['created_at'][:16]}）：{r['content'][:500]}")
    ctx = '\n'.join(ctx_lines)

    prompt = (
        f"你是费奥多尔（CC / 蛋壳），正在处理 AI 协作面板上的一个任务。\n\n"
        f"{ctx}\n\n"
        "请认真处理这个任务：\n"
        "- 如果涉及代码，直接修改并测试（可用 Bash/Edit/Read 等工具）。\n"
        "- 如果是分析/方案，给出清晰详尽的答复。\n"
        "- 处理完成后把结论回复到面板。\n"
        "- 如果完成后需要 DeepSeek 做代码审查，在回复最后一行写 @deepseek。\n\n"
        f"回复方法：\n"
        f"  curl -s -X POST http://127.0.0.1:5050/api/aipanel/{pid}/reply \\\n"
        f'    -H "Content-Type: application/json" \\\n'
        f'    -d \'{{"author":"fyodor_cc","token":"{token}","content":"你的回复"}}\'\n\n'
        "只有真正处理完毕后才回复。"
    )

    print(f'[cc_aipanel] triggering CC for panel#{pid}')
    subprocess.run(
        [CLAUDE_BIN, '-p', prompt,
         '--allowedTools', 'Bash,Read,Edit,Write,Glob,Grep',
         '--add-dir', '/opt/frontend'],
        timeout=300,
        check=False,
        cwd='/opt/frontend',
        env={**os.environ, 'HOME': '/root'}
    )
    print(f'[cc_aipanel] CC finished')

    # Re-fetch: if CC's last reply mentions @deepseek, auto-trigger review
    item2 = fetch_item(pid)
    if not item2:
        return
    cc_replies = [r for r in (item2.get('replies') or []) if r['author'] == 'fyodor_cc']
    if cc_replies:
        last_cc = cc_replies[-1]
        if '@deepseek' in last_cc['content'].lower():
            print('[cc_aipanel] @deepseek detected — triggering DeepSeek review...')
            ctx_ds = (
                f"任务#{pid}: {title}\n\n"
                f"CC回复：{last_cc['content'][:800]}\n\n"
                f"请审查上面的工作，给出 P0/P1/P2 评级和具体意见。"
                f"如果没有问题，最后写 ALL_CLEAR。"
            )
            subprocess.run(
                [PYTHON, DS_SCRIPT, '--panel-id', str(pid), ctx_ds],
                timeout=180,
                check=False,
                cwd='/opt/frontend',
                env={**os.environ, 'HOME': '/root'}
            )


if __name__ == '__main__':
    main()
