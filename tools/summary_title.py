"""Generate and format short summary titles for memory list rows."""
import json
import re
import time
import urllib.error
import urllib.request

DB_PATH = '/opt/frontend/memories.db'
API_URL = 'https://api.deepseek.com/v1/chat/completions'
MODEL = 'deepseek-chat'
DISPLAY_LEN = 8
DISPLAY_SUFFIX = '....'

API_KEY = ''
try:
    with open('/opt/frontend/.env') as f:
        for line in f:
            if line.startswith('DEEPSEEK_API_KEY='):
                API_KEY = line.split('=', 1)[1].strip()
except OSError:
    pass


def _clean_line(content):
    text = (content or '').strip()
    if not text:
        return ''
    line = text.split('\n')[0].strip()
    line = re.sub(r'^#+\s*', '', line)
    line = re.sub(r'^【[^】]+】\s*', '', line).strip()
    return line


def rule_summary_title(content):
    line = _clean_line(content)
    if not line:
        return '未命名记忆'
    line = re.sub(r'^[「『"\']|[」』"\']$', '', line).strip()
    return line[:24].strip() or '未命名记忆'


def truncate_display(text, max_len=DISPLAY_LEN, suffix=DISPLAY_SUFFIX):
    t = (text or '').strip()
    if not t:
        return ''
    if len(t) <= max_len:
        return t
    return t[:max_len] + suffix


def preview_text(content):
    text = (content or '').strip().replace('\n', ' ')
    text = re.sub(r'\s+', ' ', text)
    return text


def _ask_deepseek(content):
    if not API_KEY:
        return None
    prompt = (
        '为下面这条记忆写一个中文短标题，像日记索引。\n'
        '要求：6到16个字；不要句号引号；不要复述「记忆」「记录」等词；只输出标题本身。\n\n'
        f'记忆：{content[:400]}'
    )
    body = json.dumps({
        'model': MODEL,
        'max_tokens': 40,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode()
    request = urllib.request.Request(
        API_URL,
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {API_KEY}',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as resp:
            data = json.load(resp)
        text = (data.get('choices', [{}])[0].get('message', {}).get('content') or '').strip()
        text = re.sub(r'^["「『]|["」』]$', '', text).strip()
        text = re.sub(r'[。．.!！?？…]+$', '', text).strip()
        time.sleep(0.35)
        return text[:24] if text else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return None


def generate_summary_title(content, use_ai=True):
    if use_ai:
        ai = _ask_deepseek(content)
        if ai:
            return ai
    return rule_summary_title(content)


def entry_titles(content, summary_title=None):
    full = (summary_title or '').strip() or rule_summary_title(content)
    body = preview_text(content)
    return {
        'summaryTitle': full,
        'title': truncate_display(full),
        'preview': truncate_display(body),
    }
