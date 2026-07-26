"""Filter Wake continuity injection when the user has explicitly closed a concern.

Read-only, heuristic, fail-open: callers must wrap in try/except and continue on error.
Does not mutate wake_log.consumed or posts.resolved.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

_AI_AUTHORS = frozenset({'fyodor', 'claude', 'assistant'})

# User explicitly closes a concern — assistant agreement alone does not count.
_RESOLUTION_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'已经(?:好了|没事了|解决|愈合|恢复|结案|结束|处理(?:完|好)|没事了)',
        r'(?:无需|不需|不用|不必|不要)(?:再|继续|进一步)',
        r'(?:已经|这件事).{0,16}可以放下',
        r'医生说(?:不用|不(?:用|需要)|没事)',
        r'都好了',
        r'没事了',
        r'不用再(?:问|担心|追问|管)',
        r'这件事(?:已经)?(?:结束|过去|完了)',
    )
)

# New user evidence may reopen a previously closed concern.
_REOPEN_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'又(?:恶化|严重|疼|痛|红肿|发作|感染|流血)',
        r'(?:还是|仍然|依然)(?:疼|痛|不舒服|担心|有问题|没好)',
        r'(?:又得|还是要|还是得|仍要|仍得|需要再)',
        r'(?:重新|再次)(?:问|担心|处理|去医院|看医生)',
    )
)

_STOPWORDS = frozenset({
    '已经', '可以', '不用', '无需', '没有', '什么', '怎么', '我们', '你们', '自己',
    '一下', '继续', '还是', '就是', '这个', '那个', '事情', '事项', '今天', '昨天',
    '现在', '之后', '之前', '一下', '感觉', '知道', '觉得', '告诉', '医生', '她说',
    '他说', '我说', '问我', '问你', '好了', '没事', '结束', '放下', '不用', '再问',
})

_DEFAULT_LOOKBACK_HOURS = 168  # resolution scan only; not the Wake 8h chat snippet
_TOPIC_CONTEXT_MESSAGES = 20
_MIN_TOPIC_OVERLAP = 2

_WORRY_MARKERS = re.compile(
    r'(?:还在想|担心|要不要|需不需要|悬而|放不下|仍未|还没(?:好|解决|愈合))'
)


@dataclass
class ResolutionEntry:
    summary: str
    topic_tokens: frozenset[str] = field(default_factory=frozenset)
    message_id: int | None = None
    created_at: str = ''


@dataclass
class ResolutionState:
    active: list[ResolutionEntry] = field(default_factory=list)


def topic_tokens(text: str) -> frozenset[str]:
    """Extract coarse Chinese/alpha tokens for overlap matching."""
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


def is_user_resolution(text: str) -> bool:
    body = (text or '').strip()
    if not body:
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


def _resolution_summary(text: str, *, limit: int = 120) -> str:
    body = ' '.join((text or '').split())
    if len(body) <= limit:
        return body
    return body[: limit - 1] + '…'


def _collect_resolution_topics(
    messages: Sequence[dict],
    index: int,
    *,
    window: int = _TOPIC_CONTEXT_MESSAGES,
) -> frozenset[str]:
    tokens: set[str] = set()
    start = max(0, index - window + 1)
    for msg in messages[start:index + 1]:
        tokens.update(topic_tokens(str(msg.get('content') or '')))
    return frozenset(tokens)


def _latest_resolution_time(state: ResolutionState | None) -> str:
    if not state or not state.active:
        return ''
    stamps = [entry.created_at for entry in state.active if entry.created_at]
    return max(stamps) if stamps else ''


def build_resolution_state(
    user_messages: Sequence[dict],
    *,
    min_overlap: int = _MIN_TOPIC_OVERLAP,
) -> ResolutionState:
    """Chronological scan: later user reopen removes matching active resolutions."""
    active: list[ResolutionEntry] = []
    for index, msg in enumerate(user_messages):
        content = str(msg.get('content') or '')
        if is_user_reopen(content):
            reopen_tokens = topic_tokens(content)
            active = [
                entry for entry in active
                if not topic_overlap(entry.topic_tokens, reopen_tokens, min_overlap=min_overlap)
            ]
            continue
        if not is_user_resolution(content):
            continue
        entry = ResolutionEntry(
            summary=_resolution_summary(content),
            topic_tokens=_collect_resolution_topics(user_messages, index),
            message_id=msg.get('id'),
            created_at=str(msg.get('created_at') or ''),
        )
        active.append(entry)
    return ResolutionState(active=active)


def is_superseded_historical_concern(
    text: str,
    state: ResolutionState | None,
    *,
    min_overlap: int = _MIN_TOPIC_OVERLAP,
    recorded_at: str = '',
) -> bool:
    if not state or not state.active:
        return False
    tokens = topic_tokens(text)
    if tokens:
        for entry in state.active:
            if topic_overlap(tokens, entry.topic_tokens, min_overlap=min_overlap):
                return True
            if _WORRY_MARKERS.search(text or '') and topic_overlap(
                tokens, entry.topic_tokens, min_overlap=1,
            ):
                return True
    if _WORRY_MARKERS.search(text or '') and recorded_at:
        latest = _latest_resolution_time(state)
        if latest and recorded_at < latest:
            worry_tokens = topic_tokens(text)
            for entry in state.active:
                if topic_overlap(worry_tokens, entry.topic_tokens, min_overlap=1):
                    return True
    return False


def format_resolution_guard(state: ResolutionState | None) -> str:
    if not state or not state.active:
        return ''
    lines = ['## 用户已明确结案（勿再当作悬案追问）']
    for entry in state.active:
        stamp = ''
        if entry.created_at and len(entry.created_at) >= 16:
            stamp = f'[{entry.created_at[5:16]}] '
        lines.append(f'- {stamp}{entry.summary}')
    lines.append(
        '说明：以上是她亲口给出的结论，优先于你过去的 Wake / 日记 / 推测。'
        '这些内容只能作历史背景；除非她之后给出新的相反信息，'
        '不得仅凭旧 Wake、旧日记或未消费 wake_log 重新开启追问。'
    )
    return '\n'.join(lines)


def fetch_user_messages(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, author, content, created_at
        FROM chat_messages
        WHERE author NOT IN ('fyodor', 'claude', 'assistant')
          AND created_at >= datetime('now', '+8 hours', ?)
        ORDER BY id ASC
        """,
        (f'-{int(lookback_hours)} hours',),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        if hasattr(row, 'keys'):
            out.append({
                'id': row['id'],
                'author': row['author'],
                'content': row['content'],
                'created_at': row['created_at'],
            })
        else:
            out.append({
                'id': row[0],
                'author': row[1],
                'content': row[2],
                'created_at': row[3],
            })
    return out


def load_resolution_state(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> ResolutionState:
    return build_resolution_state(fetch_user_messages(conn, lookback_hours=lookback_hours))


def filter_wake_rows(rows: Iterable, state: ResolutionState | None) -> list:
    kept = []
    for row in rows:
        content = ''
        recorded_at = ''
        if hasattr(row, 'keys'):
            content = row['content'] or ''
            recorded_at = row['woke_at'] or ''
        elif isinstance(row, dict):
            content = row.get('content') or ''
            recorded_at = row.get('woke_at') or ''
        elif isinstance(row, (list, tuple)) and len(row) >= 3:
            content = row[2] or ''
            if len(row) >= 1:
                recorded_at = row[0] or ''
        if is_superseded_historical_concern(
            content, state, recorded_at=recorded_at,
        ):
            continue
        kept.append(row)
    return kept


def filter_wake_items(items: Iterable[dict], state: ResolutionState | None) -> list[dict]:
    kept: list[dict] = []
    for item in items or ():
        if is_superseded_historical_concern(
            str(item.get('content') or ''),
            state,
            recorded_at=str(item.get('woke_at') or ''),
        ):
            continue
        kept.append(dict(item))
    return kept


def filter_diary_texts(texts: Iterable[str], state: ResolutionState | None) -> list[str]:
    return [
        text for text in texts
        if not is_superseded_historical_concern(str(text or ''), state)
    ]