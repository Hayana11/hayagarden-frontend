"""Facts-only day handoff builder for Daily Candidate Shadow diagnostics.

Builds a temporary YAML handoff from chat_messages for one chat day
(Asia/Shanghai 04:00:00 through next day 03:59:59). Output is written
under /tmp/hayagarden-clean-shadow/ only — never posts, diary, or memory.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import stat
from typing import Any, Callable, Iterable, Optional

NL = chr(10)
TZ_OFFSET_HOURS = 8
CHAT_DAY_START_HOUR = 4

HANDOFF_LIST_KEYS = (
    'topics',
    'confirmed_facts',
    'decisions',
    'open_loops',
    'explicit_user_requests',
)

HANDOFF_SCALAR_KEYS = (
    'day',
    'source_day',
    'source_start_at',
    'source_end_at',
    'source_first_message_id',
    'source_last_message_id',
    'source_message_count',
    'source_sha256',
    'extraction_mode',
    'requires_human_review',
    'last_topic',
)

HANDOFF_REQUIRED_KEYS = HANDOFF_SCALAR_KEYS + HANDOFF_LIST_KEYS

ALLOWED_TOP_LEVEL_KEYS = frozenset(HANDOFF_LIST_KEYS + HANDOFF_SCALAR_KEYS)

SHADOW_HANDOFF_DIR = '/tmp/hayagarden-clean-shadow'
HANDOFF_FILENAME_PREFIX = 'day_handoff_'
HANDOFF_FILENAME_SUFFIX = '.yaml'

MAX_FILE_BYTES = 32_768
MAX_LIST_ITEMS = 12
MAX_ITEM_CHARS = 240
MAX_LAST_TOPIC_CHARS = 320

_BEHAVIOR_MARKERS = (
    '你应该', '语气', '怎么回复', '怎么提', '表现得', '顺嘴',
    '请回复', '请把它视为', '抱着她说', '不得复述', '不得改变语气',
)

_ACTION_MARKERS = (
    '（抱', '（揽', '（亲', '【动作', '[动作',
)

_QUOTE_PATTERNS = (
    re.compile(r'[「『""](.{6,}?)[」』""]'),
    re.compile(r'“(.{6,}?)”'),
    re.compile(r'"(.{6,}?)"'),
)

_DECISION_HINTS = ('决定', '确定', '就这样', '好的我们', '定了', '采纳')
_REQUEST_HINTS = ('请', '不许', '不要', '帮我', '记得', '务必', '需要')
_FACT_HINTS = (
    '部署', '合并', 'PR', '完成', '版本', '上线', '修复', '发布',
    '号', '日', '月', '年', '%', '元', '点', '分',
)
_CASUAL_MARKERS = (
    '喵喵', '哈哈', '嗯嗯', '抱抱', '亲亲', '～', '~', '…',
)
_USER_AUTHORS = frozenset({'hayana', 'user'})
_AI_AUTHORS = frozenset({'fyodor', 'claude', 'assistant'})

_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_ASSISTANT_NARRATION_MARKERS = (
    '我轻轻', '我抱着', '我揽着', '我低声', '我轻声',
)
_SPEAKER_LABEL_PATTERNS = (
    re.compile(r'助手回应'),
    re.compile(r'助手[:：]'),
    re.compile(r'(?:^|[；;，,\s])assistant[:：]\s*', re.IGNORECASE),
    re.compile(r'(?:^|[；;，,\s])claude[:：]\s*', re.IGNORECASE),
    re.compile(r'(?:^|[；;，,\s])fyodor[:：]\s*', re.IGNORECASE),
)
_CONFIRMATION_MARKERS = (
    '好', '好的', '可以', '没问题', '就这样', '嗯', '行', '收到', '明白',
)


def _now_local() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS)


def validate_day_string(day_str: str) -> str:
    day = str(day_str or '').strip()
    if not _DAY_RE.match(day):
        raise ValueError('day must be YYYY-MM-DD')
    datetime.datetime.strptime(day, '%Y-%m-%d')
    return day


def chat_day_for_timestamp(ts: datetime.datetime) -> str:
    """Map a +8-local timestamp to its chat day (04:00 boundary)."""
    if ts.hour < CHAT_DAY_START_HOUR:
        ts = ts - datetime.timedelta(days=1)
    return ts.strftime('%Y-%m-%d')


def chat_day_str(*, offset_days: int = -1, now: Optional[datetime.datetime] = None) -> str:
    """Chat day in Asia/Shanghai; default previous chat day."""
    base = chat_day_for_timestamp(now or _now_local())
    day = datetime.datetime.strptime(base, '%Y-%m-%d').date()
    if offset_days:
        day = day + datetime.timedelta(days=offset_days)
    return day.strftime('%Y-%m-%d')


def chat_day_window(day_str: str) -> tuple[str, str, str, str]:
    """Return (source_day, start_at, end_at, next_start_at) for a chat day."""
    day = validate_day_string(day_str)
    start = datetime.datetime.strptime(day + ' 04:00:00', '%Y-%m-%d %H:%M:%S')
    next_start = start + datetime.timedelta(days=1)
    end = next_start - datetime.timedelta(seconds=1)
    return (
        day,
        start.strftime('%Y-%m-%d %H:%M:%S'),
        end.strftime('%Y-%m-%d %H:%M:%S'),
        next_start.strftime('%Y-%m-%d %H:%M:%S'),
    )


def _row_author(row: Any) -> str:
    if hasattr(row, 'keys'):
        return str(row['author'] or '')
    return str(getattr(row, 'author', '') or '')


def _row_id(row: Any) -> int:
    if hasattr(row, 'keys'):
        return int(row['id'] or 0)
    return int(getattr(row, 'id', 0) or 0)


def _content(row: Any) -> str:
    if hasattr(row, 'keys'):
        return str(row['content'] or '').strip()
    return str(getattr(row, 'content', '') or '').strip()


def _created_at(row: Any) -> str:
    if hasattr(row, 'keys'):
        return str(row['created_at'] or '')
    return str(getattr(row, 'created_at', '') or '')


def _normalize_note(text: str, *, max_len: int = MAX_ITEM_CHARS) -> str:
    """Strip dialogue/action; keep a short neutral note without quoting."""
    t = str(text or '').strip()
    t = re.sub(r'[「『""“].*?[」』""”]', '', t)
    t = re.sub(r'（[^）]{0,40}）', '', t)
    t = re.sub(r'\[[^\]]{0,40}\]', '', t)
    t = re.sub(r'\s+', ' ', t).strip(' ，。；;')
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + '…'
    return t


def _looks_casual(text: str) -> bool:
    t = str(text or '').strip()
    if len(t) < 8:
        return True
    if any(m in t for m in _CASUAL_MARKERS):
        return True
    if re.fullmatch(r'[\W\d_a-zA-Z\u4e00-\u9fff]{0,20}', t) and len(t) < 16:
        return True
    return False


def _looks_like_fact_candidate(text: str) -> bool:
    t = str(text or '').strip()
    if _looks_casual(t):
        return False
    if '?' in t or '？' in t:
        return False
    if not any(h in t for h in _FACT_HINTS):
        return False
    return len(t) >= 12


def _is_question(text: str) -> bool:
    t = str(text or '').strip()
    return ('?' in t or '？' in t or t.endswith('吗') or t.endswith('么'))


def _assistant_answered(rows: list[Any], question_index: int) -> bool:
    for row in rows[question_index + 1:]:
        author = _row_author(row)
        if author in _USER_AUTHORS:
            return False
        if author in _AI_AUTHORS and _content(row):
            return True
    return False


def _looks_like_narrow_confirmation(text: str) -> bool:
    t = str(text or '').strip()
    if not t or len(t) > 40:
        return False
    return any(t == m or t.startswith(m) for m in _CONFIRMATION_MARKERS)


def _contains_assistant_voice(text: str) -> bool:
    v = str(text or '').strip()
    if not v:
        return False
    for marker in _ASSISTANT_NARRATION_MARKERS:
        if marker in v:
            return True
    for pat in _SPEAKER_LABEL_PATTERNS:
        if pat.search(v):
            return True
    return False


def _detect_joint_decision(rows: list[Any], index: int) -> Optional[str]:
    row = rows[index]
    if _row_author(row) not in _USER_AUTHORS:
        return None
    text = _content(row)
    if not any(h in text for h in _DECISION_HINTS):
        return None
    confirmed = False
    for follow in rows[index + 1:index + 4]:
        author = _row_author(follow)
        if author in _USER_AUTHORS:
            return None
        if author in _AI_AUTHORS and _content(follow):
            if not _looks_like_narrow_confirmation(_content(follow)):
                return None
            confirmed = True
            break
    if not confirmed:
        return None
    note = _normalize_note(text, max_len=120)
    return note or None


def _source_sha256(rows: list[Any]) -> str:
    parts = []
    for row in rows:
        parts.append('%s|%s|%s|%s' % (
            _row_id(row), _row_author(row), _created_at(row), _content(row),
        ))
    return hashlib.sha256(NL.join(parts).encode('utf-8')).hexdigest()


def fetch_day_messages(
    get_db_fn: Callable[[], Any],
    day_str: str,
) -> list[Any]:
    source_day, start_at, end_at, next_start_at = chat_day_window(day_str)
    conn = get_db_fn()
    try:
        return list(conn.execute(
            'SELECT id, author, content, created_at FROM chat_messages '
            'WHERE created_at >= ? AND created_at < ? ORDER BY id ASC',
            (start_at, next_start_at),
        ).fetchall())
    finally:
        conn.close()


def build_day_handoff_from_messages(
    rows: list[Any],
    *,
    day_str: str = '',
) -> dict[str, Any]:
    """Conservative rule extraction; empty sections preferred over guessing."""
    source_day, start_at, end_at, _next_start_at = chat_day_window(day_str or chat_day_str())
    users = [r for r in rows if _row_author(r) in _USER_AUTHORS]
    user_texts = [_content(r) for r in users if _content(r)]

    topics: list[str] = []
    try:
        import jieba
        freq: dict[str, int] = {}
        for text in user_texts:
            for word in jieba.cut(text):
                w = word.strip()
                if len(w) < 2 or w in ('爸爸', '小猫', '哈娅', '费佳', '今天', '一下', '我们'):
                    continue
                freq[w] = freq.get(w, 0) + 1
        topics = [w for w, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:6]]
    except Exception:
        topics = []

    confirmed_facts: list[str] = []
    for text in user_texts:
        if not _looks_like_fact_candidate(text):
            continue
        note = _normalize_note(text)
        if note and note not in confirmed_facts:
            confirmed_facts.append(note)
    confirmed_facts = confirmed_facts[:MAX_LIST_ITEMS]

    decisions: list[str] = []
    for i, row in enumerate(rows):
        joint = _detect_joint_decision(rows, i)
        if joint and joint not in decisions:
            decisions.append(joint)
    decisions = decisions[:MAX_LIST_ITEMS]

    open_loops: list[str] = []
    for i, row in enumerate(rows):
        if _row_author(row) not in _USER_AUTHORS:
            continue
        text = _content(row)
        if not _is_question(text):
            continue
        if _assistant_answered(rows, i):
            continue
        note = _normalize_note(text)
        if note and note not in open_loops:
            open_loops.append(note)
    open_loops = open_loops[:MAX_LIST_ITEMS]

    explicit_user_requests: list[str] = []
    for text in user_texts:
        if not any(h in text for h in _REQUEST_HINTS):
            continue
        note = _normalize_note(text)
        if note and note not in explicit_user_requests:
            explicit_user_requests.append(note)
    explicit_user_requests = explicit_user_requests[:MAX_LIST_ITEMS]

    last_topic = ''
    if rows:
        user_bits: list[str] = []
        for row in reversed(rows[-10:]):
            if _row_author(row) not in _USER_AUTHORS:
                continue
            note = _normalize_note(_content(row), max_len=80)
            if note:
                user_bits.append(note)
            if len(user_bits) >= 3:
                break
        last_topic = '；'.join(reversed(user_bits))[:MAX_LAST_TOPIC_CHARS]

    first_id = _row_id(rows[0]) if rows else 0
    last_id = _row_id(rows[-1]) if rows else 0

    return {
        'day': source_day,
        'source_day': source_day,
        'source_start_at': start_at,
        'source_end_at': end_at,
        'source_first_message_id': first_id,
        'source_last_message_id': last_id,
        'source_message_count': len(rows),
        'source_sha256': _source_sha256(rows),
        'extraction_mode': 'conservative_rules',
        'requires_human_review': True,
        'topics': topics,
        'confirmed_facts': confirmed_facts,
        'decisions': decisions,
        'open_loops': open_loops,
        'explicit_user_requests': explicit_user_requests,
        'last_topic': last_topic,
    }


def _parse_non_negative_int(value: Any, path: str, errors: list[str]) -> Optional[int]:
    if isinstance(value, bool):
        errors.append('%s: must be a non-negative integer' % path)
        return None
    if isinstance(value, int):
        n = value
    else:
        s = str(value or '').strip()
        if not s.isdigit():
            errors.append('%s: must be a non-negative integer' % path)
            return None
        n = int(s)
    if n < 0:
        errors.append('%s: must be a non-negative integer' % path)
        return None
    return n


def validate_day_handoff(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ['handoff must be a mapping']

    for key in HANDOFF_REQUIRED_KEYS:
        if key not in data:
            errors.append('missing key: %s' % key)

    unknown = set(data.keys()) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        errors.append('unknown keys: %s' % ', '.join(sorted(unknown)))

    if data.get('extraction_mode') != 'conservative_rules':
        errors.append('extraction_mode must be conservative_rules')

    if data.get('requires_human_review') not in (True, 'true', 'True', 1):
        errors.append('requires_human_review must be true')

    day = str(data.get('day') or '').strip()
    source_day = str(data.get('source_day') or '').strip()
    if day and source_day and day != source_day:
        errors.append('day and source_day must match')
    for label, value in (('day', day), ('source_day', source_day)):
        if value and not _DAY_RE.match(value):
            errors.append('%s: must be YYYY-MM-DD' % label)
    if source_day:
        try:
            expected_day, expected_start, expected_end, _next_start = chat_day_window(source_day)
        except ValueError:
            errors.append('source_day: invalid chat day')
            expected_day = expected_start = expected_end = ''
        else:
            if day and day != expected_day:
                errors.append('day does not match 04:00 chat-day window')
            if str(data.get('source_start_at') or '') != expected_start:
                errors.append('source_start_at does not match 04:00 chat-day window')
            if str(data.get('source_end_at') or '') != expected_end:
                errors.append('source_end_at does not match 04:00 chat-day window')

    sha = str(data.get('source_sha256') or '').strip().lower()
    if not _SHA256_RE.match(sha):
        errors.append('source_sha256 must be 64 lowercase hex characters')

    first_id = _parse_non_negative_int(
        data.get('source_first_message_id'), 'source_first_message_id', errors,
    )
    last_id = _parse_non_negative_int(
        data.get('source_last_message_id'), 'source_last_message_id', errors,
    )
    msg_count = _parse_non_negative_int(
        data.get('source_message_count'), 'source_message_count', errors,
    )
    if first_id is not None and last_id is not None and msg_count is not None:
        if msg_count == 0:
            if first_id != 0 or last_id != 0:
                errors.append('source message ids must be 0 when count is 0')
        else:
            if first_id <= 0 or last_id <= 0:
                errors.append('source message ids must be positive when count > 0')
            elif first_id > last_id:
                errors.append('source_first_message_id must be <= source_last_message_id')

    def _check_value(path: str, value: str):
        v = str(value or '')
        if not v.strip():
            return
        if _contains_assistant_voice(v):
            errors.append('%s: contains assistant voice' % path)
        if len(v) > MAX_ITEM_CHARS and path != 'last_topic':
            errors.append('%s: exceeds max item length' % path)
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
        if re.search(r'^我(?!们)[^。]{7,}', v):
            errors.append('%s: first-person assistant-style narration' % path)

    last_topic = str(data.get('last_topic') or '')
    if len(last_topic) > MAX_LAST_TOPIC_CHARS:
        errors.append('last_topic: exceeds max length')
    _check_value('last_topic', last_topic)

    for key in HANDOFF_LIST_KEYS:
        val = data.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            errors.append('%s: must be a list' % key)
            continue
        if len(val) > MAX_LIST_ITEMS:
            errors.append('%s: too many items (%d)' % (key, len(val)))
        for i, item in enumerate(val):
            if not isinstance(item, str):
                errors.append('%s[%d]: must be string' % (key, i))
                continue
            _check_value('%s[%d]' % (key, i), item)

    return errors


def format_day_handoff_yaml(data: dict[str, Any]) -> str:
    def _yaml_list(key: str, items: list[str]) -> list[str]:
        lines = ['%s:' % key]
        if not items:
            return lines
        for item in items:
            escaped = str(item).replace('"', '\\"')
            lines.append('  - "%s"' % escaped)
        return lines

    lines: list[str] = []
    for scalar in HANDOFF_SCALAR_KEYS:
        if scalar not in data:
            continue
        val = data[scalar]
        if scalar in HANDOFF_LIST_KEYS:
            continue
        if isinstance(val, bool):
            lines.append('%s: %s' % (scalar, 'true' if val else 'false'))
        else:
            escaped = str(val).replace('"', '\\"')
            lines.append('%s: "%s"' % (scalar, escaped))
    for key in HANDOFF_LIST_KEYS:
        lines.extend(_yaml_list(key, list(data.get(key) or [])))
    return NL.join(lines) + NL


def format_day_handoff_prompt(data: dict[str, Any]) -> str:
    yaml_text = format_day_handoff_yaml(data).strip()
    return (
        '【昨日交接·事实记录】\n'
        '以下字段记录昨日已经确认的信息，用于保持话题连续性。\n\n'
        + yaml_text
    )


def _stat_owned_path(path: str, *, expect_dir: bool = False) -> os.stat_result:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError('path must not be a symlink: %s' % path)
    if expect_dir:
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError('expected directory: %s' % path)
    elif not stat.S_ISREG(st.st_mode):
        raise ValueError('expected regular file: %s' % path)
    if st.st_uid != os.geteuid():
        raise ValueError('path owner mismatch: %s' % path)
    return st


def ensure_shadow_handoff_dir() -> str:
    if os.path.exists(SHADOW_HANDOFF_DIR):
        _stat_owned_path(SHADOW_HANDOFF_DIR, expect_dir=True)
    else:
        os.makedirs(SHADOW_HANDOFF_DIR, mode=0o700, exist_ok=True)
    os.chmod(SHADOW_HANDOFF_DIR, stat.S_IRWXU)
    _stat_owned_path(SHADOW_HANDOFF_DIR, expect_dir=True)
    return SHADOW_HANDOFF_DIR


def _validate_handoff_filename(name: str) -> str:
    base = os.path.basename(str(name or ''))
    if not base.startswith(HANDOFF_FILENAME_PREFIX):
        raise ValueError('invalid handoff filename prefix')
    if not base.endswith(HANDOFF_FILENAME_SUFFIX):
        raise ValueError('invalid handoff filename suffix')
    if base != name or '..' in base or '/' in base or '\\' in base:
        raise ValueError('invalid handoff filename')
    return base


def resolve_shadow_handoff_path(path: str) -> str:
    ensure_shadow_handoff_dir()
    raw = str(path or '')
    if os.path.islink(raw):
        raise ValueError('day_handoff path must not be a symlink')
    parent = os.path.dirname(os.path.abspath(raw))
    _stat_owned_path(parent, expect_dir=True)
    candidate = os.path.realpath(raw)
    base = os.path.realpath(SHADOW_HANDOFF_DIR)
    if os.path.commonpath([candidate, base]) != base:
        raise ValueError('day_handoff path must be under %s' % SHADOW_HANDOFF_DIR)
    _validate_handoff_filename(os.path.basename(candidate))
    if not os.path.exists(candidate):
        raise FileNotFoundError(candidate)
    _stat_owned_path(candidate, expect_dir=False)
    return candidate


def _read_regular_file_no_follow(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode):
            raise ValueError('day_handoff file must not be a symlink')
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('day_handoff file must be a regular file')
        if st.st_uid != os.geteuid():
            raise ValueError('day_handoff file owner mismatch')
        with os.fdopen(fd, 'r', encoding='utf-8') as fh:
            fd = None
            return fh.read()
    finally:
        if fd is not None:
            os.close(fd)


def parse_day_handoff_yaml(text: str) -> dict[str, Any]:
    """Strict parser: required keys, no unknown keys, no duplicate keys."""
    if len(text.encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('handoff file too large')

    seen_keys: set[str] = set()
    data: dict[str, Any] = {}
    current_key: Optional[str] = None

    for lineno, raw_line in enumerate(str(text or '').splitlines(), start=1):
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith('#'):
            continue

        if line.strip().startswith('-'):
            if current_key not in HANDOFF_LIST_KEYS:
                raise ValueError('list item without list key at line %d' % lineno)
            item = line.strip()[1:].strip().strip('"').strip("'")
            if not item:
                continue
            data.setdefault(current_key, []).append(item)
            continue

        if ':' not in line:
            raise ValueError('invalid line %d' % lineno)

        key, raw_val = line.split(':', 1)
        key = key.strip()
        if key not in ALLOWED_TOP_LEVEL_KEYS:
            raise ValueError('unknown key %r at line %d' % (key, lineno))
        if key in seen_keys:
            raise ValueError('duplicate key %r at line %d' % (key, lineno))
        seen_keys.add(key)

        val = raw_val.strip()
        if key in HANDOFF_LIST_KEYS:
            if val:
                raise ValueError('list key %r must not have inline value' % key)
            data[key] = []
            current_key = key
            continue

        current_key = None
        if val.lower() in ('true', 'false'):
            data[key] = val.lower() == 'true'
        else:
            data[key] = val.strip('"').strip("'")

    for key in HANDOFF_REQUIRED_KEYS:
        if key not in data:
            raise ValueError('missing required key: %s' % key)
    for key in HANDOFF_LIST_KEYS:
        if key in data and not isinstance(data[key], list):
            raise ValueError('%s must be a list' % key)
    return data


def load_day_handoff_from_path(path: str) -> dict[str, Any]:
    real = resolve_shadow_handoff_path(path)
    text = _read_regular_file_no_follow(real)
    return parse_day_handoff_yaml(text)


def load_and_validate_day_handoff(path: str) -> tuple[dict[str, Any], list[str]]:
    data = load_day_handoff_from_path(path)
    errors = validate_day_handoff(data)
    if errors:
        raise ValueError('day_handoff validation failed: ' + '; '.join(errors))
    return data, errors


def write_day_handoff_to_tmp(
    data: dict[str, Any],
    *,
    prefix: str = HANDOFF_FILENAME_PREFIX,
) -> str:
    errors = validate_day_handoff(data)
    if errors:
        raise ValueError('day_handoff validation failed: ' + '; '.join(errors))

    ensure_shadow_handoff_dir()
    day = str(data.get('source_day') or data.get('day') or chat_day_str()).replace('-', '')
    filename = _validate_handoff_filename('%s%s%s' % (prefix, day, HANDOFF_FILENAME_SUFFIX))
    final_path = os.path.join(SHADOW_HANDOFF_DIR, filename)

    if os.path.lexists(final_path):
        raise FileExistsError('refusing to overwrite existing handoff: %s' % final_path)

    fd = os.open(
        final_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fd = None
            fh.write(format_day_handoff_yaml(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(final_path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(final_path) and os.path.getsize(final_path) == 0:
            try:
                os.remove(final_path)
            except OSError:
                pass
    return final_path


def build_and_write_yesterday_handoff(
    get_db_fn: Callable[[], Any],
    *,
    day_str: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    day = validate_day_string(day_str or chat_day_str(offset_days=-1))
    rows = fetch_day_messages(get_db_fn, day)
    data = build_day_handoff_from_messages(rows, day_str=day)
    path = write_day_handoff_to_tmp(data)
    return path, data


# Backward-compatible alias used in early draft code/tests.
calendar_day_str = chat_day_str
