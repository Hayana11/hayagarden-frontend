"""Filter Wake continuity injection when the user has explicitly closed a concern.

Read-only, heuristic, fail-open: callers must wrap DB access in try/finally.
Does not mutate wake_log.consumed or posts.resolved.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

_USER_AUTHORS = frozenset({'hayana', 'haya', 'user'})

_RESOLUTION_NEGATION = re.compile(
    r'(?:才不是|并不是|并非|没有|别|别想|不算|哪能|哪能是|哪是)'
    r'.{0,10}(?:没事了|结束了|都好了|不用了|可以放下|已经好了)',
)
_RESOLUTION_RHETORICAL = re.compile(
    r'(?:你以为|难道|是不是|难道就|怎么就).{0,12}(?:结束了|没事了|都好了|不用了)\??',
)

_RESOLUTION_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'已经(?:好了|解决|愈合|恢复|结案|处理(?:完|好))',
        r'(?:无需|不需|不用|不必|不要)(?:再|继续|进一步)',
        r'(?:已经|这件事).{0,16}可以放下',
        r'医生说(?:不用|不(?:用|需要)|没事)',
        r'不用再(?:问|担心|追问|管)',
        r'这件事(?:已经)?(?:结束|过去|完了)',
        r'(?:伤口|抓伤|磕伤|烫伤).{0,12}(?:已经|早就)(?:好了|愈合|恢复)',
        r'(?:咨询|问过).{0,6}医生.{0,16}(?:不用|不(?:用|需要)|没事)',
    )
)

_REOPEN_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'又(?:恶化|严重|疼|痛|红肿|发作|感染|流血)',
        r'(?:还是|仍然|依然)(?:疼|痛|不舒服|担心|有问题|没好)',
        r'(?:又得|还是要|还是得|仍要|仍得|需要再)',
        r'(?:重新|再次)(?:问|担心|处理|去医院|看医生)',
        r'(?:今天|刚才|刚刚).{0,8}(?:红肿|渗液|发炎|疼|痛|恶化)',
        r'(?:开始|出现)(?:红肿|渗液|发炎|疼痛|恶化)',
    )
)

_GENERIC_TOPIC_TOKENS = frozenset({
    '已经', '可以', '不用', '无需', '没有', '什么', '怎么', '我们', '你们', '自己',
    '一下', '继续', '还是', '就是', '这个', '那个', '事情', '事项', '今天', '昨天',
    '现在', '之后', '之前', '感觉', '知道', '觉得', '告诉', '医生', '她说', '他说',
    '我说', '问我', '问你', '好了', '没事', '结束', '放下', '再问', '这件事', '处理',
    '完了', '过去', '结案', '不用了', '没事了', '都好了', '进一步', '再跑', '再问',
})

_DEFAULT_LOOKBACK_HOURS = 168
_MIN_TOPIC_OVERLAP = 2
_GUARD_MAX_ENTRIES = 3

_STOPWORDS = _GENERIC_TOPIC_TOKENS


@dataclass(frozen=True)
class ResolutionEntry:
    summary: str
    topic_tokens: frozenset[str] = field(default_factory=frozenset)
    message_id: int | None = None
    created_at: str = ''


@dataclass
class ResolutionState:
    active: list[ResolutionEntry] = field(default_factory=list)


@dataclass
class FilterResult:
    kept: list = field(default_factory=list)
    applied_resolutions: list[ResolutionEntry] = field(default_factory=list)


def topic_tokens(text: str) -> frozenset[str]:
    text = (text or '').strip().lower()
    if not text:
        return frozenset()
    tokens: set[str] = set()
    for chunk in re.split(r'[^\u4e00-\u9fffA-Za-z0-9]+', text):
        if len(chunk) < 2:
            continue
        if chunk not in _STOPWORDS:
            tokens.add(chunk)
        if len(chunk) >= 3:
            for i in range(len(chunk) - 1):
                bigram = chunk[i:i + 2]
                if bigram not in _STOPWORDS:
                    tokens.add(bigram)
    return frozenset(tokens)


def _topic_specific_tokens(text: str) -> frozenset[str]:
    return frozenset(token for token in topic_tokens(text) if token not in _GENERIC_TOPIC_TOKENS)


_DEICTIC_RESOLUTION = re.compile(
    r'(?:这件事|那件事|这事|那事|不用了|没事了|都好了|处理好了|可以放下|结束了)',
)

def _needs_prior_user_context(content: str) -> bool:
    body = (content or '').strip()
    if _DEICTIC_RESOLUTION.search(body):
        return True
    return len(_topic_specific_tokens(content)) < _MIN_TOPIC_OVERLAP


def is_user_resolution(text: str) -> bool:
    body = (text or '').strip()
    if not body:
        return False
    if _RESOLUTION_NEGATION.search(body) or _RESOLUTION_RHETORICAL.search(body):
        return False
    return any(p.search(body) for p in _RESOLUTION_PATTERNS)


def is_user_reopen(text: str) -> bool:
    body = (text or '').strip()
    if not body:
        return False
    return any(p.search(body) for p in _REOPEN_PATTERNS)


def topic_overlap(left: frozenset[str], right: frozenset[str], *, min_overlap: int = _MIN_TOPIC_OVERLAP) -> bool:
    if not left or not right:
        return False
    return len(left & right) >= min_overlap


def normalize_recorded_at(value: str) -> str | None:
    value = (value or '').strip()
    if not value:
        return None
    if len(value) >= 19 and value[4] == '-' and value[7] == '-':
        return value[:19]
    if len(value) >= 16 and value[4] == '-' and value[7] == '-':
        return value[:16] + ':00'
    return None


def is_strictly_before(source_at: str, resolution_at: str) -> bool | None:
    """Return True/False when comparable; None => fail-open (do not filter)."""
    source = normalize_recorded_at(source_at)
    resolution = normalize_recorded_at(resolution_at)
    if not source or not resolution:
        return None
    return source < resolution


def _resolution_summary(text: str, *, limit: int = 120) -> str:
    body = ' '.join((text or '').split())
    if len(body) <= limit:
        return body
    return body[: limit - 1] + '…'


def _collect_resolution_topics(messages: Sequence[dict], index: int) -> frozenset[str]:
    content = str(messages[index].get('content') or '')
    tokens = set(topic_tokens(content))
    if _needs_prior_user_context(content) and index > 0:
        tokens.update(topic_tokens(str(messages[index - 1].get('content') or '')))
    return frozenset(tokens)


def build_resolution_state(
    user_messages: Sequence[dict],
    *,
    min_overlap: int = _MIN_TOPIC_OVERLAP,
) -> ResolutionState:
    active: list[ResolutionEntry] = []
    for index, msg in enumerate(user_messages):
        content = str(msg.get('content') or '')
        if is_user_reopen(content):
            reopen_tokens = topic_tokens(content)
            active = [
                entry for entry in active
                if not topic_overlap(entry.topic_tokens, reopen_tokens, min_overlap=1)
            ]
            continue
        if not is_user_resolution(content):
            continue
        active.append(ResolutionEntry(
            summary=_resolution_summary(content),
            topic_tokens=_collect_resolution_topics(user_messages, index),
            message_id=msg.get('id'),
            created_at=str(msg.get('created_at') or ''),
        ))
    return ResolutionState(active=active)


def find_superseding_resolution(
    text: str,
    state: ResolutionState | None,
    *,
    recorded_at: str = '',
    min_overlap: int = _MIN_TOPIC_OVERLAP,
) -> ResolutionEntry | None:
    if not state or not state.active:
        return None
    tokens = topic_tokens(text)
    if not tokens:
        return None
    for entry in state.active:
        if not topic_overlap(tokens, entry.topic_tokens, min_overlap=min_overlap):
            continue
        before = is_strictly_before(recorded_at, entry.created_at)
        if before is not True:
            continue
        return entry
    return None


def is_superseded_historical_concern(
    text: str,
    state: ResolutionState | None,
    *,
    recorded_at: str = '',
    min_overlap: int = _MIN_TOPIC_OVERLAP,
) -> bool:
    return find_superseding_resolution(
        text, state, recorded_at=recorded_at, min_overlap=min_overlap,
    ) is not None


def dedupe_applied_resolutions(
    entries: Iterable[ResolutionEntry],
    *,
    max_items: int = _GUARD_MAX_ENTRIES,
) -> list[ResolutionEntry]:
    ordered = sorted(
        entries,
        key=lambda entry: normalize_recorded_at(entry.created_at) or '',
        reverse=True,
    )
    picked: list[ResolutionEntry] = []
    for entry in ordered:
        if len(picked) >= max_items:
            break
        if any(topic_overlap(entry.topic_tokens, kept.topic_tokens) for kept in picked):
            continue
        picked.append(entry)
    return list(reversed(picked))


def format_resolution_guard(applied: Sequence[ResolutionEntry]) -> str:
    entries = dedupe_applied_resolutions(applied)
    if not entries:
        return ''
    lines = ['## 用户已明确结案（勿再当作悬案追问）']
    for entry in entries:
        stamp = ''
        normalized = normalize_recorded_at(entry.created_at)
        if normalized:
            stamp = f'[{normalized[5:16]}] '
        lines.append(f'- {stamp}{entry.summary}')
    lines.append(
        '说明：以上是她亲口给出的结论，优先于你过去的 Wake / 日记 / 推测。'
        '这些内容只能作历史背景；除非她之后给出新的相反信息，'
        '不得仅凭旧 Wake、旧日记或未消费 wake_log 重新开启追问。'
    )
    return '\n'.join(lines)


def _is_user_author(author: str) -> bool:
    return str(author or '').strip().lower() in _USER_AUTHORS


def fetch_user_messages(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, author, content, created_at
        FROM chat_messages
        WHERE created_at >= datetime('now', '+8 hours', ?)
        ORDER BY id ASC
        """,
        (f'-{int(lookback_hours)} hours',),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        if hasattr(row, 'keys'):
            author = row['author']
            item = {
                'id': row['id'],
                'author': author,
                'content': row['content'],
                'created_at': row['created_at'],
            }
        else:
            author = row[1]
            item = {
                'id': row[0],
                'author': author,
                'content': row[2],
                'created_at': row[3],
            }
        if not _is_user_author(author):
            continue
        out.append(item)
    return out


def load_resolution_state(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> ResolutionState:
    return build_resolution_state(fetch_user_messages(conn, lookback_hours=lookback_hours))


def load_resolution_state_from_db(get_db_fn, *, lookback_hours: int = _DEFAULT_LOOKBACK_HOURS) -> ResolutionState:
    conn = get_db_fn()
    try:
        return load_resolution_state(conn, lookback_hours=lookback_hours)
    finally:
        conn.close()


def _row_content_and_time(row) -> tuple[str, str]:
    if hasattr(row, 'keys'):
        keys = row.keys()
        if 'woke_at' in keys:
            return str(row['content'] or ''), str(row['woke_at'] or '')
        return str(row['content'] or ''), str(row['created_at'] or '')
    if isinstance(row, dict):
        if 'woke_at' in row:
            return str(row.get('content') or ''), str(row.get('woke_at') or '')
        return str(row.get('content') or ''), str(row.get('created_at') or '')
    if isinstance(row, (list, tuple)) and len(row) >= 3:
        return str(row[2] or ''), str(row[0] or '')
    return str(row or ''), ''


def filter_wake_rows(rows: Iterable, state: ResolutionState | None) -> FilterResult:
    kept = []
    applied: list[ResolutionEntry] = []
    for row in rows:
        content, recorded_at = _row_content_and_time(row)
        match = find_superseding_resolution(content, state, recorded_at=recorded_at)
        if match:
            applied.append(match)
            continue
        kept.append(row)
    return FilterResult(kept=kept, applied_resolutions=applied)


def filter_wake_items(items: Iterable[dict], state: ResolutionState | None) -> FilterResult:
    kept: list[dict] = []
    applied: list[ResolutionEntry] = []
    for item in items or ():
        content = str(item.get('content') or '')
        recorded_at = str(item.get('woke_at') or '')
        match = find_superseding_resolution(content, state, recorded_at=recorded_at)
        if match:
            applied.append(match)
            continue
        kept.append(dict(item))
    return FilterResult(kept=kept, applied_resolutions=applied)


def filter_diary_rows(rows: Iterable, state: ResolutionState | None) -> FilterResult:
    kept = []
    applied: list[ResolutionEntry] = []
    for row in rows:
        content, recorded_at = _row_content_and_time(row)
        match = find_superseding_resolution(content, state, recorded_at=recorded_at)
        if match:
            applied.append(match)
            continue
        kept.append(row)
    return FilterResult(kept=kept, applied_resolutions=applied)
