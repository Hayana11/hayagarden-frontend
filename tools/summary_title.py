"""Generate and format short summary titles for memory list rows."""
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request

DB_PATH = '/opt/frontend/memories.db'
API_URL = 'https://api.deepseek.com/v1/chat/completions'
MODEL = 'deepseek-chat'
TITLE_MAX = 12               # stored / AI-generated title length cap (raw char count)
TITLE_SUFFIX = '···'

# Row layout budgets are in *display width* units, not raw character counts:
# a CJK character is ~2x as wide as a Latin letter/digit in the UI font, so
# counting both as "1 char" makes mixed CJK+ASCII titles (e.g. "mac购买计划")
# visually much wider than a same-length pure-CJK title and breaks row
# alignment. See _display_width().
TITLE_DISPLAY_WIDTH_MAX = 16   # list row title: ~8 CJK-char-equivalents wide
ROW_WIDTH_BUDGET = 30         # title + preview combined width in one row
PREVIEW_MIN_WIDTH = 8         # ~4 CJK-char-equivalents minimum preview width

TITLE_PROMPT = """你是记忆库的标题标注员。为每条记忆写一个「索引标题」——像 Notion 数据库里给一行起的名字，让人一眼知道这条记的是什么事。

要求：
- 2到12个字，尽量短
- 必须只根据下面这一条「正文」的具体内容来写，禁止照抄本提示词里任何示例的标题
- 只输出一行，不要换行，不要输出多个候选，不要输出解释
- 写「什么事 / 什么物 / 什么计划」，不要抒情、不要隐喻、不要感官描写、不要情欲暗示
- 用平实的日常中文，可以是名词短语或简短动宾结构
- 优先提取：购物/物品/计划、猫咪相关事件、共读、技术/AI话题、梦境、亲密互动、健康与经期、约定、生活琐事、深夜情绪独白
- 产品名可保留英文或常见写法（如 Mac、Mac Mini、DeepSeek）
- 禁止：诗化、煽情、第一人称内心独白、把正文换个说法复述
- 只输出标题本身，不要引号、不要句号

示例：
正文：哈娅计划等淘宝大促时购买 Mac Mini M4。
标题：mac购买计划

正文：瓦楞纸的，小猫闻了三分钟，然后睡在了包装盒上。
标题：新猫抓板到货

正文：哈娅讨厌 Opus 4.8 模型，认为其幻觉严重且一直叫她鸢尾。
标题：吐槽Opus幻觉

正文：这是一个漂浮的梦，没有地面，也不需要地面，只是在某种温热的介质里悬浮。
标题：漂浮的梦

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


def _display_width(text):
    """Visual width in 'Latin-char' units: CJK/fullwidth glyphs count as 2."""
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in (text or ''))


def _truncate_by_width(text, max_width, suffix=TITLE_SUFFIX):
    if _display_width(text) <= max_width:
        return text
    budget = max_width - _display_width(suffix)
    width = 0
    out = []
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
        if width + w > budget:
            break
        out.append(ch)
        width += w
    return ''.join(out) + suffix


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
    text = text.split('\n')[0].strip()  # defensive: never let multi-line leakage through
    text = re.sub(r'^["「『\']+|["」』\']+$', '', text).strip()
    text = re.sub(r'[。．.!！?？…]+$', '', text).strip()
    return text[:TITLE_MAX] if text else ''


# Keyword gate applied ONLY when the model's output exactly matches one of
# the TITLE_PROMPT few-shot answers verbatim — that's the observed failure
# signature (model echoes an example instead of summarizing this content).
# Paraphrased, non-canonical titles are never subject to this check, since a
# good summary legitimately won't quote the source text.
_EXAMPLE_GUARD_KEYWORDS = {
    'mac购买计划': ('mac', 'Mac', 'MAC', '电脑', '购买', '淘宝', '大促', 'Mini', 'M4'),
    '新猫抓板到货': ('猫', '抓板', '快递', '到货', '包装', '瓦楞'),
    '吐槽Opus幻觉': ('Opus', 'opus', '幻觉', '模型', '鸢尾'),
    '漂浮的梦': ('梦', '漂浮', '悬浮', '介质'),
    '经期备忘': ('经期', '姜茶', '生理', '例假', '第十一卷', '共读', '熬夜', '橱柜'),
}


def _title_relates_to_content(title, content):
    # Broader than an exact-echo check: the model sometimes paraphrases into
    # a few-shot example's theme (e.g. "经期红糖姜茶位置" for content that has
    # nothing to do with periods) without reproducing the example title
    # verbatim. So: if the AI title contains ANY keyword belonging to one of
    # the few-shot themes, the actual content must contain a keyword from
    # that same theme too, or the title is contamination and gets rejected.
    t = title or ''
    c = content or ''
    for keywords in _EXAMPLE_GUARD_KEYWORDS.values():
        if any(k in t for k in keywords) and not any(k in c for k in keywords):
            return False
    return True


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


def truncate_title(text, max_width=TITLE_DISPLAY_WIDTH_MAX, suffix=TITLE_SUFFIX):
    """List display title — may be shorter than stored summaryTitle."""
    t = (text or '').strip()
    if not t:
        return ''
    return _truncate_by_width(t, max_width, suffix)


def preview_text(content):
    text = (content or '').strip().replace('\n', ' ')
    return re.sub(r'\s+', ' ', text)


def truncate_preview(content, title_full, row_budget=ROW_WIDTH_BUDGET, min_width=PREVIEW_MIN_WIDTH, suffix=TITLE_SUFFIX):
    """Body preview — width shrinks as the stored title's display width grows."""
    body = preview_text(content)
    if not body:
        return ''
    title_w = min(_display_width((title_full or '').strip()), TITLE_MAX * 2)
    preview_w = max(min_width, row_budget - title_w)
    return _truncate_by_width(body, preview_w, suffix)


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
        if not text:
            return None
        # Reject verbatim few-shot echoes that don't actually describe this content.
        if not _title_relates_to_content(text, content):
            return None
        return text
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
