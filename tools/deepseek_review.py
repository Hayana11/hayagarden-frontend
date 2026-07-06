#!/usr/bin/env python3
"""
手动调用工具：把代码/方案丢给 DeepSeek 要一段评审意见 + P0/P1/P2 严重度。
给正在实际工作的 Claude Code 会话用——写完代码主动调一次，不挂 cron，不被任何路由自动触发。

用法：
  python3 deepseek_review.py "一段方案说明或diff"
  git diff | python3 deepseek_review.py
"""
import sys, re, argparse, requests

DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
MODEL        = 'deepseek-reasoner'

LEVEL_RE = re.compile(r'\[(P0|P1|P2)\]')

SYSTEM_PROMPT = (
    '你是代码评审者 DeepSeek。针对给出的代码/diff/方案，给出具体、简洁的评审意见——'
    '哪里有bug、哪里可以更简单、有没有遗漏的边界情况，不要寒暄。'
    '最后单独一行写严重度标签，格式必须是 [P0]、[P1] 或 [P2] 三者之一：'
    'P0=有明确bug/会出问题，必须改；P1=有改进空间，建议改；P2=没问题/只是次要建议。'
)


def _load_env():
    ds_key = ''
    for line in open('/opt/frontend/.env'):
        k, _, v = line.partition('=')
        k, v = k.strip(), v.strip()
        if k == 'DEEPSEEK_API_KEY':
            ds_key = v
    return ds_key


def review(content, ds_key):
    r = requests.post(
        DEEPSEEK_URL,
        headers={'Authorization': f'Bearer {ds_key}', 'Content-Type': 'application/json'},
        json={
            'model': MODEL,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': content},
            ]
        },
        timeout=180
    )
    r.raise_for_status()
    raw = r.json()['choices'][0]['message']['content'].strip()
    m = LEVEL_RE.search(raw)
    level = m.group(1) if m else 'P2'
    text = LEVEL_RE.sub('', raw).strip()
    return text, level


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('content', nargs='?', help='要评审的内容，留空则从 stdin 读取')
    args = ap.parse_args()

    content = args.content
    if not content:
        content = sys.stdin.read()
    content = (content or '').strip()
    if not content:
        print('没有给内容，退出', file=sys.stderr)
        sys.exit(1)

    ds_key = _load_env()
    if not ds_key:
        print('未配置 DEEPSEEK_API_KEY', file=sys.stderr)
        sys.exit(1)

    text, level = review(content, ds_key)
    print(f'[{level}] {text}')


if __name__ == '__main__':
    main()
