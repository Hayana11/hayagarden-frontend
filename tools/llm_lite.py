"""轻量 LLM 调用共享助手（DeepSeek 直连）。
tag_enricher / fact_extractor / summarizer 等后台 cron 共用，
不再各自复制一份 _ask()。热路径（gateway）不要用这个——它是同步阻塞的。
"""
import json
import re
import time
import urllib.request as _req

API_URL = 'https://api.deepseek.com/v1/chat/completions'
MODEL = 'deepseek-chat'

_API_KEY = None


def _key():
    global _API_KEY
    if _API_KEY is None:
        _API_KEY = ''
        try:
            for line in open('/opt/frontend/.env'):
                if line.startswith('DEEPSEEK_API_KEY='):
                    _API_KEY = line.split('=', 1)[1].strip()
        except Exception:
            pass
    return _API_KEY


def ask(prompt, max_tokens=600, timeout=90, expect_json=False):
    """调 DeepSeek，返回文本；expect_json=True 时抽取并解析第一个 JSON 值。
    失败返回 ''（或 expect_json 时返回 None），不抛异常。"""
    key = _key()
    if not key:
        return None if expect_json else ''
    body = json.dumps({
        'model': MODEL, 'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
    }, ensure_ascii=False).encode('utf-8')
    req = _req.Request(API_URL, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + key,
    })
    try:
        with _req.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        text = (data.get('choices', [{}])[0].get('message', {}).get('content') or '').strip()
        time.sleep(0.3)  # 温和限速，别打爆余额告警
        if expect_json:
            m = re.search(r'[\[{].*[\]}]', text, re.DOTALL)
            if not m:
                return None
            try:
                return json.loads(m.group())
            except Exception:
                return None
        return text
    except Exception:
        return None if expect_json else ''
