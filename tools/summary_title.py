"""Generate and format short summary titles for memory list rows."""
import json
import re
import time
import urllib.error
import urllib.request

DB_PATH = '/opt/frontend/memories.db'
API_URL = 'https://api.deepseek.com/v1/chat/completions'
MODEL = 'deepseek-chat'
TITLE_MAX = 12          # stored / AI-generated title length cap
TITLE_DISPLAY_MAX = 8   # list row: show up to 8 chars, then ···
TITLE_SUFFIX = '···'
ROW_CHAR_BUDGET = 15    # title(full len) + preview(visible chars) ≈ budget
PREVIEW_MIN = 4

TITLE_PROMPT = """以下是一段日记，用一句中文，为这段日记做一个标题式的总结，捕捉这段思考里最主要的事件或最浓烈的情感状态。

语言简练、诗意、有力度，用具体的动词和感官词汇呈现情感本身的温度和质地，让读的人脑子里能出现一个画面。当思考中有对她身体的渴望、占有、色情的念头和冲动时，总结也要诚实地带着同等浓度的情欲，不把欲望包装成抒情。禁止直白的情绪词堆砌，要口语化表达。

以感受或内心动作开头，第一人称视角但省略主语"我"。用陈述语气或动词短语，不超过12字。

日记：
{content}"""

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


def _normalize_title(text):
    text = (text or '').strip()
    text = re.sub(r'^["「『\']+|["」』\']+$', '', text).strip()
    text = re.sub(r'[。．.!！?？…]+$', '', text).strip()
    return text[:TITLE_MAX] if text else ''


def rule_summary_title(content):
    text = (content or '').strip()
    for pat in (r'[「『]([^」』]{1,14})[」』]', r'《([^》]{1,12})》'):
        m = re.search(pat, text)
        if m:
            return _normalize_title(m.group(1)) or '未命名'
    line = _clean_line(text)
    for sep in ('，', '。', '；', '：', ','):
        if sep in line:
            head = line.split(sep)[0].strip()
            if len(head) >= 2:
                return _normalize_title(head)
    return _normalize_title(line) or '未命名'


def truncate_title(text, max_len=TITLE_DISPLAY_MAX, suffix=TITLE_SUFFIX):
    """List display title — may be shorter than stored summaryTitle."""
    t = (text or '').strip()
    if not t:
        return ''
    if len(t) <= max_len:
        return t
    return t[:max_len] + suffix


def preview_text(content):
    text = (content or '').strip().replace('\n', ' ')
    return re.sub(r'\s+', ' ', text)


def truncate_preview(content, title_full, row_budget=ROW_CHAR_BUDGET, min_preview=PREVIEW_MIN, suffix=TITLE_SUFFIX):
    """Body preview — length shrinks as the stored title grows (up to TITLE_MAX)."""
    body = preview_text(content)
    if not body:
        return ''
    title_len = min(len((title_full or '').strip()), TITLE_MAX)
    preview_len = max(min_preview, row_budget - title_len)
    if len(body) <= preview_len:
        return body
    return body[:preview_len] + suffix


def _ask_deepseek(content):
    if not API_KEY:
        return None
    prompt = TITLE_PROMPT.format(content=(content or '')[:800])
    body = json.dumps({
        'model': MODEL,
        'max_tokens': 48,
        'temperature': 0.7,
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
        with urllib.request.urlopen(request, timeout=60) as resp:
            data = json.load(resp)
        text = (data.get('choices', [{}])[0].get('message', {}).get('content') or '').strip()
        text = _normalize_title(text)
        time.sleep(0.4)
        return text or None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, OSError):
        return None


def generate_summary_title(content, use_ai=True):
    if use_ai:
        ai = _ask_deepseek(content)
        if ai:
            return ai
    return rule_summary_title(content)


def entry_titles(content, summary_title=None):
    full = (summary_title or '').strip() or rule_summary_title(content)
    return {
        'summaryTitle': full,
        'title': truncate_title(full),
        'preview': truncate_preview(content, full),
    }
