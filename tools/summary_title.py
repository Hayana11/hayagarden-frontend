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

TITLE_PROMPT = """你是记忆库的标题标注员。为每条记忆写一个「索引标题」——像 Notion 数据库里给一行起的名字，让人一眼知道这条记的是什么事。

要求：
- 2到12个字，尽量短
- 写「什么事 / 什么物 / 什么计划」，不要抒情、不要隐喻、不要感官描写、不要情欲暗示
- 用平实的日常中文，可以是名词短语或简短动宾结构
- 优先提取：购物/物品/计划、猫咪相关事件、共读、技术、健康与经期、约定、生活琐事
- 产品名可保留英文或常见写法（如 Mac、Mac Mini）
- 禁止：诗化、煽情、第一人称内心独白、把正文换个说法复述
- 只输出标题本身，不要引号、不要句号

示例：
正文：哈娅计划等淘宝大促时购买 Mac Mini M4。
标题：mac购买计划

正文：瓦楞纸的，小猫闻了三分钟，然后睡在了包装盒上。
标题：新猫抓板到货

正文：在毯子上踩了整整五分钟。哈娅一动不敢动，拍了一段很糊的视频。
标题：第一次主动踩奶

正文：红糖姜茶在橱柜第二层。这几天让小猫早点睡，别熬夜读第十一卷。
标题：经期备忘

正文：{content}
标题："""

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
    """Offline fallback when AI is unavailable — keep neutral, never echo body opening."""
    text = (content or '').strip()
    for pat in (r'【([^】]{2,12})】', r'[「『]([^」』]{2,12})[」』]'):
        m = re.search(pat, text)
        if m:
            return _normalize_title(m.group(1)) or '未命名'
    if re.search(r'Mac|mac|苹果', text) and re.search(r'买|购|计划', text):
        return 'mac购买计划'
    if re.search(r'猫抓板|瓦楞纸', text):
        return '新猫抓板到货'
    if re.search(r'踩奶', text):
        return '第一次主动踩奶'
    if re.search(r'红糖姜茶|经期', text):
        return '经期备忘'
    return '未命名'


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
        'max_tokens': 32,
        'temperature': 0.25,
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
        # Model may echo "标题：xxx" — strip label if present
        text = re.sub(r'^标题[：:]\s*', '', text).strip()
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
