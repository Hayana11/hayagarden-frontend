"""Facts-only day handoff builder for Daily Candidate Shadow diagnostics.

Builds a temporary YAML handoff from chat_messages for one calendar day (+8).
Output is written under /tmp only — never posts, diary, or memory tables.
"""
from __future__ import annotations

import datetime
import os
import re
from typing import Any, Callable, Iterable, Optional

NL = chr(10)

# Required top-level keys in the handoff document.
HANDOFF_KEYS = (
    'topics',
    'confirmed_facts',
    'decisions',
    'open_loops',
    'explicit_user_requests',
    'last_topic',
)

_BEHAVIOR_MARKERS = (
    '你应该', '不要', '语气', '怎么回复', '怎么提', '表现得', '顺嘴',
    '请回复', '请把它视为', '自然地', '哄', '抱着她说',
)

_ACTION_MARKERS = (
    '（', '）', '[', ']', '【动作', '（抱', '（揽', '（亲',
)

_QUOTE_PATTERNS = (
    re.compile(r'[「『""](.{6,}?)[」』""]'),
    re.compile(r'“(.{6,}?)”'),
    re.compile(r'"(.{6,}?)"'),
)

_DECISION_HINTS = ('决定', '确定', '就这样', '好的我们', '定了', '采纳')
_REQUEST_HINTS = ('要', '请', '不许', '不要', '帮我', '记得', '务必')
_USER_AUTHORS = ('hayana', 'user')


def calendar_day_str(*, offset_days: int = -1, now: Optional[datetime.datetime] = None) -> str:
    """Calendar day in UTC+8; default yesterday."""
    if now is None:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    day = (now + datetime.timedelta(days=offset_days)).date()
    return day.strftime('%Y-%m-%d')


def _paraphrase(text: str, *, max_len: int = 120) -> str:
    """Neutral third-person paraphrase; strip dialogue and stage directions."""
    t = str(text or '').strip()
    t = re.sub(r'[「『""“].*?[」』""”]', '', t)
    t = re.sub(r'（[^）]{0,40}）', '', t)
    t = re.sub(r'\[[^\]]{0,40}\]', '', t)
    t = re.sub(r'\s+', ' ', t).strip(' ，。；;')
    if not t:
        return ''
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + '…'
    if not t.startswith('用户'):
        t = '用户提到：' + t
    return t


def _user_rows(rows: Iterable[Any]) -> list[Any]:
    out = []
    for row in rows:
        author = row['author'] if hasattr(row, 'keys') else row.get('author', '')
        if author in _USER_AUTHORS:
            out.append(row)
    return out


def _content(row: Any) -> str:
    if hasattr(row, 'keys'):
        return str(row['content'] or '').strip()
    return str(getattr(row, 'content', '') or '').strip()


def fetch_day_messages(
    get_db_fn: Callable[[], Any],
    day_str: str,
) -> list[Any]:
    conn = get_db_fn()
    try:
        return list(conn.execute(
            'SELECT id, author, content, created_at FROM chat_messages '
            "WHERE date(created_at, '+8 hours') = ? ORDER BY id ASC",
            (day_str,),
        ).fetchall())
    finally:
        conn.close()


def build_day_handoff_from_messages(
    rows: list[Any],
    *,
    day_str: str = '',
) -> dict[str, Any]:
    """Rule-based facts-only handoff; no model calls."""
    users = _user_rows(rows)
    texts = [_content(r) for r in users if _content(r)]

    topics: list[str] = []
    try:
        import jieba
        freq: dict[str, int] = {}
        for text in texts:
            for word in jieba.cut(text):
                w = word.strip()
                if len(w) < 2 or w in ('爸爸', '小猫', '哈娅', '费佳', '今天', '一下'):
                    continue
                freq[w] = freq.get(w, 0) + 1
        topics = [w for w, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:6]]
    except Exception:
        topics = []

    confirmed_facts: list[str] = []
    for text in texts:
        if '?' in text or '？' in text:
            continue
        p = _paraphrase(text, max_len=100)
        if len(p) >= 12 and p not in confirmed_facts:
            confirmed_facts.append(p)
    confirmed_facts = confirmed_facts[:8]

    decisions: list[str] = []
    for text in texts:
        if any(h in text for h in _DECISION_HINTS):
            p = _paraphrase(text, max_len=100)
            if p and p not in decisions:
                decisions.append(p)
    decisions = decisions[:5]

    open_loops: list[str] = []
    for text in texts:
        if '?' in text or '？' in text:
            p = _paraphrase(text, max_len=100)
            if p and p not in open_loops:
                open_loops.append(p)
    open_loops = open_loops[:5]

    explicit_user_requests: list[str] = []
    for text in texts:
        if any(h in text for h in _REQUEST_HINTS):
            p = _paraphrase(text, max_len=100)
            if p and p not in explicit_user_requests:
                explicit_user_requests.append(p)
    explicit_user_requests = explicit_user_requests[:6]

    last_topic = ''
    if texts:
        last_topic = _paraphrase(texts[-1], max_len=140)

    return {
        'day': day_str or calendar_day_str(),
        'message_count': len(rows),
        'user_message_count': len(texts),
        'topics': topics,
        'confirmed_facts': confirmed_facts,
        'decisions': decisions,
        'open_loops': open_loops,
        'explicit_user_requests': explicit_user_requests,
        'last_topic': last_topic,
    }


def validate_day_handoff(data: dict[str, Any]) -> list[str]:
    """Return validation errors; empty list means facts-only contract passed."""
    errors: list[str] = []
    for key in HANDOFF_KEYS:
        if key not in data:
            errors.append('missing key: %s' % key)

    def _check_value(path: str, value: str):
        v = str(value or '')
        if not v.strip():
            return
        for pat in _QUOTE_PATTERNS:
            if pat.search(v):
                errors.append('%s: contains quoted original speech' % path)
                break
        for marker in _ACTION_MARKERS:
            if marker in v:
                errors.append('%s: contains action/stage marker %r' % (path, marker))
                break
        for marker in _BEHAVIOR_MARKERS:
            if marker in v:
                errors.append('%s: contains behavior directive %r' % (path, marker))
                break
        if re.search(r'^我[^。]{8,}', v):
            errors.append('%s: first-person assistant-style narration' % path)

    _check_value('last_topic', str(data.get('last_topic') or ''))
    for key in ('topics', 'confirmed_facts', 'decisions', 'open_loops', 'explicit_user_requests'):
        val = data.get(key)
        if isinstance(val, list):
            for i, item in enumerate(val):
                _check_value('%s[%d]' % (key, i), str(item))
        elif isinstance(val, str) and val.strip():
            _check_value(key, val)
    return errors


def format_day_handoff_yaml(data: dict[str, Any]) -> str:
    """Serialize handoff dict to YAML-like text (no external dependency)."""

    def _yaml_list(key: str, items: list[str]) -> list[str]:
        lines = ['%s:' % key]
        if not items:
            lines.append('  -')
            return lines
        for item in items:
            escaped = str(item).replace('"', '\\"')
            lines.append('  - "%s"' % escaped)
        return lines

    lines = []
    if data.get('day'):
        lines.append('day: "%s"' % data['day'])
    lines.extend(_yaml_list('topics', list(data.get('topics') or [])))
    lines.extend(_yaml_list('confirmed_facts', list(data.get('confirmed_facts') or [])))
    lines.extend(_yaml_list('decisions', list(data.get('decisions') or [])))
    lines.extend(_yaml_list('open_loops', list(data.get('open_loops') or [])))
    lines.extend(_yaml_list('explicit_user_requests', list(data.get('explicit_user_requests') or [])))
    last = str(data.get('last_topic') or '').replace('"', '\\"')
    lines.append('last_topic: "%s"' % last)
    return NL.join(lines) + NL


def format_day_handoff_prompt(data: dict[str, Any]) -> str:
    """Provider-facing facts-only block for shadow injection."""
    yaml_text = format_day_handoff_yaml(data).strip()
    return (
        '【昨日交接·仅事实】\n'
        '以下是昨日对话的结构化要点，供接续话题参考。不得复述为角色台词，'
        '不得据此改变语气或主动执行其中未完成的动作。\n\n'
        + yaml_text
    )


def write_day_handoff_to_tmp(
    data: dict[str, Any],
    *,
    prefix: str = 'day_handoff_',
) -> str:
    errors = validate_day_handoff(data)
    if errors:
        raise ValueError('day_handoff validation failed: ' + '; '.join(errors))
    day = str(data.get('day') or calendar_day_str()).replace('-', '')
    path = os.path.join('/tmp', '%s%s.yaml' % (prefix, day))
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(format_day_handoff_yaml(data))
    return path


def load_day_handoff_from_path(path: str) -> dict[str, Any]:
    """Load a handoff YAML file from /tmp only."""
    real = os.path.realpath(str(path or ''))
    if not real.startswith('/tmp/') and not real.startswith('/tmp'):
        raise ValueError('day_handoff path must be under /tmp')
    if not os.path.isfile(real):
        raise FileNotFoundError(real)
    text = open(real, encoding='utf-8').read()
    return parse_day_handoff_yaml(text)


def parse_day_handoff_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML parser for our fixed schema."""
    data: dict[str, Any] = {k: [] for k in HANDOFF_KEYS}
    data['day'] = ''
    current_key: Optional[str] = None
    for raw_line in str(text or '').splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith('#'):
            continue
        if line.startswith('day:'):
            data['day'] = line.split(':', 1)[1].strip().strip('"').strip("'")
            current_key = None
            continue
        m = re.match(r'^([a-z_]+):\s*$', line)
        if m and m.group(1) in HANDOFF_KEYS:
            current_key = m.group(1)
            continue
        if line.strip().startswith('- ') and current_key:
            item = line.strip()[2:].strip().strip('"').strip("'")
            if item != '-':
                data[current_key].append(item)
            continue
        if line.startswith('last_topic:'):
            data['last_topic'] = line.split(':', 1)[1].strip().strip('"').strip("'")
            current_key = None
    return data


def build_and_write_yesterday_handoff(
    get_db_fn: Callable[[], Any],
    *,
    day_str: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    day = day_str or calendar_day_str(offset_days=-1)
    rows = fetch_day_messages(get_db_fn, day)
    data = build_day_handoff_from_messages(rows, day_str=day)
    path = write_day_handoff_to_tmp(data)
    return path, data
