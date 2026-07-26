"""Filter Wake continuity when the user has explicitly closed a concern.

Persisted closures live in ``concern_closures`` (survive beyond chat lookback).
Recent user chat is scanned to add/reopen closures; active rows drive filtering.
Fail-open on parse/DB errors. Does not mutate wake_log.consumed or posts.resolved.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

_USER_AUTHORS = frozenset({'hayana', 'haya', 'user'})

_RESOLUTION_NEGATION = re.compile(
    r'(?:才不是|并不是|并非|这不算|哪能是|哪是)'
    r'.{0,8}(?:没事了|结束了|都好了|不用了|可以放下|已经好了|处理好了)',
)
_RESOLUTION_RHETORICAL = re.compile(
    r'(?:你以为|难道|是不是|难道就|怎么就).{0,12}(?:结束了|没事了|都好了|不用了)\??',
)

_RESOLUTION_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'已经(?:好了|没事了|解决|处理(?:完|好)|取完|送到|到账|修好|恢复|完成|跟完|愈合|好转)',
        r'已经.{0,8}(?:愈合|好转|恢复|解决|处理(?:完|好)|取完|跟完)',
        r'又.{0,8}(?:好了|修好|送到|取完|取到|到账|完成|解决|处理(?:完|好)|跟完|愈合)',
        r'(?:无需|不需|不用|不必|不要)(?:再|继续|进一步|跑|催|问|打|管|担心)',
        r'(?:已经|这件事).{0,16}可以放下',
        r'不用再(?:问|担心|追问|管|跑|催|打)',
        r'这件事(?:已经)?(?:结束|过去|完了)',
    )
)

_REOPEN_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'又(?:出|坏|失败|报错|延迟|问题|恶化|反复|卡住|停|漏|找不到)',
        r'(?:还是|仍然|依然)(?:没|不|有)(?:好|行|完成|解决|取到|修好|到账|送到)',
        r'(?:又得|还是要|还是得|仍要|仍得|需要再|还得)',
        r'(?:重新|再次)(?:出现|发生|报错|出问题|问|处理|催|跑|取)',
        r'(?:今天|刚才|刚刚).{0,12}(?:出问题|报错|失败|恶化|坏了|找不到|延迟|开始|又)',
        r'(?:开始|出现)(?:问题|故障|报错|状况|反复|了|着)',
        r'(?:仍|还)(?:有|没|未).{0,8}(?:问题|故障|报错|完成|解决)',
    )
)

_DOMAIN_GENERIC = frozenset({
    '进度', '订单', '工厂', '服务', '故障', '部署', '问题', '系统', '项目',
    '前端', '后端', '接口', '模块', '版本', '环境', '厂家', '产线',
})

_STATE_TOKENS = frozenset({
    '完成', '解决', '恢复', '修好', '送到', '到账', '取到', '愈合', '好转',
    '取完', '跟完', '发货', '失败', '报错', '排查', '处理', '正常', '没事',
    '好了', '结束', '完了', '过去', '放下', '送达',
})

_GENERIC_TOPIC_TOKENS = frozenset({
    '已经', '可以', '不用', '无需', '没有', '什么', '怎么', '我们', '你们', '自己',
    '一下', '继续', '还是', '就是', '这个', '那个', '事情', '事项', '今天', '昨天',
    '现在', '之后', '之前', '感觉', '知道', '觉得', '告诉', '她说', '他说', '我说',
    '问我', '问你', '好了', '没事', '结束', '放下', '再问', '这件事', '处理', '完了',
    '过去', '结案', '不用了', '没事了', '都好了', '进一步', '再跑', '再问', '担心',
    '有点', '还没', '已经取', '取完',
})

_DEFAULT_LOOKBACK_HOURS = 168
_GUARD_MAX_ENTRIES = 3
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS concern_closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    topic_tokens TEXT NOT NULL,
    source_message_id INTEGER UNIQUE,
    resolved_at TEXT NOT NULL,
    reopened_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_concern_closures_active
    ON concern_closures(active, resolved_at);
CREATE TABLE IF NOT EXISTS concern_closure_sync (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_processed_message_id INTEGER NOT NULL DEFAULT 0
);
"""

_STOPWORDS = _GENERIC_TOPIC_TOKENS


@dataclass(frozen=True)
class ResolutionEntry:
    summary: str
    topic_tokens: frozenset[str] = field(default_factory=frozenset)
    message_id: int | None = None
    created_at: str = ''
    closure_id: int | None = None


@dataclass
class ResolutionState:
    active: list[ResolutionEntry] = field(default_factory=list)


@dataclass
class FilterResult:
    kept: list = field(default_factory=list)
    applied_resolutions: list[ResolutionEntry] = field(default_factory=list)
    suppressed_ids: list[int] = field(default_factory=list)


def ensure_concern_closure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)


def ensure_concern_closure_schema_for_path(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        ensure_concern_closure_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _normalize_for_topic_extraction(text: str) -> str:
    text = (text or '').strip().lower()
    text = re.sub(r'[，。！？、；：\s]+', ' ', text)
    text = re.sub(
        r'(?:已经|不用|无需|可以|还是|仍然|依然|记得|提醒|医生|说|想|帮|你|我|她|他|我们|你们|'
        r'的|了|在|还|要|是|有|没|不|再|又|也|就|都|和|与|及|或|而|但|却|这|那|哪)',
        ' ',
        text,
    )
    return re.sub(r'\s+', ' ', text).strip()


def topic_tokens(text: str) -> frozenset[str]:
    text = _normalize_for_topic_extraction(text)
    if not text:
        return frozenset()
    tokens: set[str] = set()
    for chunk in re.findall(
        r'[\u4e00-\u9fff]+[a-z0-9]*|[a-z0-9]+(?:-[a-z0-9]+)*',
        text,
    ):
        chunk = chunk.strip()
        if len(chunk) >= 2 and chunk not in _STOPWORDS:
            tokens.add(chunk)
        if len(chunk) >= 4 and re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            for size in (3, 4):
                for i in range(len(chunk) - size + 1):
                    piece = chunk[i:i + size]
                    if len(piece) >= 3 and piece not in _STOPWORDS:
                        tokens.add(piece)
    return frozenset(tokens)


def identity_topic_tokens(text: str) -> frozenset[str]:
    return frozenset(
        token for token in topic_tokens(text)
        if token not in _STATE_TOKENS
    )


def _identity_tokens(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(
        token for token in tokens
        if len(token) >= 2
        and token not in _GENERIC_TOPIC_TOKENS
        and token not in _DOMAIN_GENERIC
        and token not in _STATE_TOKENS
    )


def _matchable_tokens(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(
        token for token in tokens
        if len(token) >= 2
        and token not in _GENERIC_TOPIC_TOKENS
        and token not in _STATE_TOKENS
    )


def _anchor_token_match(left_token: str, right_token: str) -> bool:
    if left_token in _DOMAIN_GENERIC or right_token in _DOMAIN_GENERIC:
        return False
    if left_token == right_token:
        return True
    shorter, longer = (left_token, right_token) if len(left_token) <= len(right_token) else (right_token, left_token)
    if len(shorter) < 2 or shorter not in longer:
        return False
    if re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', shorter) and '-' not in shorter and '-' in longer:
        return False
    return True


def substantive_topic_overlap(left: frozenset[str], right: frozenset[str]) -> bool:
    left_identity = _identity_tokens(left)
    right_identity = _identity_tokens(right)
    if left_identity & right_identity:
        return True
    left_all = _matchable_tokens(left)
    right_all = _matchable_tokens(right)
    for left_token in left_all:
        for right_token in right_all:
            if _anchor_token_match(left_token, right_token):
                return True
    shared_all = left_all & right_all
    if not shared_all:
        return False
    shared_distinctive = shared_all - _DOMAIN_GENERIC
    if shared_distinctive:
        return True
    return len(shared_all) >= 2


_DEICTIC_PHRASE = re.compile(r'(?:这件事|那件事|这事|那事)')


def _needs_prior_chat_context(content: str) -> bool:
    body = (content or '').strip()
    if _DEICTIC_PHRASE.search(body):
        return True
    return len(_identity_tokens(topic_tokens(content))) < 1


def _looks_like_question(body: str) -> bool:
    if '?' in body or '？' in body:
        return True
    trimmed = body.rstrip('。.!！… ')
    return trimmed.endswith('吗')


def _last_match_position(body: str, patterns: Sequence[re.Pattern]) -> int:
    last = -1
    for pattern in patterns:
        for match in pattern.finditer(body):
            last = max(last, match.start())
    return last


def classify_user_concern_event(text: str) -> str | None:
    body = (text or '').strip()
    if not body:
        return None
    if _looks_like_question(body):
        return None
    if _RESOLUTION_NEGATION.search(body) or _RESOLUTION_RHETORICAL.search(body):
        return None
    resolution_pos = _last_match_position(body, _RESOLUTION_PATTERNS)
    reopen_pos = _last_match_position(body, _REOPEN_PATTERNS)
    if resolution_pos < 0 and reopen_pos < 0:
        return None
    if reopen_pos > resolution_pos:
        return 'reopen'
    if resolution_pos > reopen_pos:
        return 'resolve'
    return 'reopen' if reopen_pos >= 0 else 'resolve'


def is_user_resolution(text: str) -> bool:
    return classify_user_concern_event(text) == 'resolve'


def is_user_reopen(text: str) -> bool:
    return classify_user_concern_event(text) == 'reopen'


def topic_overlap(left: frozenset[str], right: frozenset[str], *, min_overlap: int = 1) -> bool:
    if substantive_topic_overlap(left, right):
        return True
    if min_overlap <= 1:
        return False
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


def _is_user_author(author: str) -> bool:
    return str(author or '').strip().lower() in _USER_AUTHORS


def _row_to_message(row) -> dict:
    if hasattr(row, 'keys'):
        return {
            'id': row['id'],
            'author': row['author'],
            'content': row['content'],
            'created_at': row['created_at'],
        }
    return {
        'id': row[0],
        'author': row[1],
        'content': row[2],
        'created_at': row[3],
    }


def fetch_chat_messages(
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
    return [_row_to_message(row) for row in rows]


def fetch_user_messages(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> list[dict]:
    return [
        msg for msg in fetch_chat_messages(conn, lookback_hours=lookback_hours)
        if _is_user_author(msg.get('author'))
    ]


def _collect_resolution_topics(chat_messages: Sequence[dict], message_id: int) -> frozenset[str]:
    index = next(
        (i for i, msg in enumerate(chat_messages) if int(msg.get('id') or 0) == int(message_id)),
        -1,
    )
    if index < 0:
        return frozenset()
    content = str(chat_messages[index].get('content') or '')
    tokens = set(identity_topic_tokens(content))
    if _needs_prior_chat_context(content) and index > 0:
        tokens.update(identity_topic_tokens(str(chat_messages[index - 1].get('content') or '')))
    return frozenset(tokens)


def _closure_row_to_entry(row) -> ResolutionEntry:
    try:
        tokens = frozenset(json.loads(row['topic_tokens'] or '[]'))
    except (TypeError, ValueError, json.JSONDecodeError):
        tokens = frozenset()
    return ResolutionEntry(
        summary=str(row['summary'] or ''),
        topic_tokens=tokens,
        message_id=row['source_message_id'],
        created_at=str(row['resolved_at'] or ''),
        closure_id=int(row['id']),
    )


def load_active_closure_state(conn: sqlite3.Connection) -> ResolutionState:
    rows = conn.execute(
        """
        SELECT id, summary, topic_tokens, source_message_id, resolved_at
        FROM concern_closures
        WHERE active=1
        ORDER BY resolved_at ASC, id ASC
        """
    ).fetchall()
    active = [_closure_row_to_entry(row) for row in rows]
    return ResolutionState(active=active)


def _deactivate_matching_closures(
    conn: sqlite3.Connection,
    reopen_tokens: frozenset[str],
    *,
    reopened_at: str,
) -> None:
    rows = conn.execute(
        """
        SELECT id, topic_tokens
        FROM concern_closures
        WHERE active=1
        """
    ).fetchall()
    for row in rows:
        try:
            tokens = frozenset(json.loads(row['topic_tokens'] or '[]'))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if substantive_topic_overlap(reopen_tokens, tokens):
            conn.execute(
                """
                UPDATE concern_closures
                SET active=0, reopened_at=?
                WHERE id=? AND active=1
                """,
                (reopened_at, int(row['id'])),
            )


def _persist_closure(conn: sqlite3.Connection, entry: ResolutionEntry) -> None:
    if entry.message_id is not None:
        existing = conn.execute(
            "SELECT id FROM concern_closures WHERE source_message_id=?",
            (int(entry.message_id),),
        ).fetchone()
        if existing:
            return
    conn.execute(
        """
        INSERT INTO concern_closures (
            summary, topic_tokens, source_message_id, resolved_at, active
        ) VALUES (?, ?, ?, ?, 1)
        """,
        (
            entry.summary,
            json.dumps(sorted(entry.topic_tokens), ensure_ascii=False),
            entry.message_id,
            entry.created_at,
        ),
    )


def _get_sync_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT last_processed_message_id FROM concern_closure_sync WHERE id=1"
    ).fetchone()
    if not row:
        return 0
    return int(row[0] if not hasattr(row, 'keys') else row['last_processed_message_id'])


def _set_sync_cursor(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute(
        """
        INSERT INTO concern_closure_sync (id, last_processed_message_id)
        VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_processed_message_id=excluded.last_processed_message_id
        """,
        (int(message_id),),
    )


def _fetch_messages_up_to(conn: sqlite3.Connection, message_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, author, content, created_at
        FROM chat_messages
        WHERE id <= ?
        ORDER BY id ASC
        """,
        (int(message_id),),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def _fetch_incremental_chat_messages(
    conn: sqlite3.Connection,
    *,
    after_message_id: int,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, author, content, created_at
        FROM chat_messages
        WHERE id > ?
        ORDER BY id ASC
        """,
        (int(after_message_id),),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def _apply_concern_event(
    conn: sqlite3.Connection,
    msg: dict,
    chat_messages: Sequence[dict],
) -> None:
    content = str(msg.get('content') or '')
    created_at = str(msg.get('created_at') or '')
    message_id = int(msg.get('id') or 0)
    if is_user_reopen(content):
        _deactivate_matching_closures(
            conn,
            identity_topic_tokens(content),
            reopened_at=created_at,
        )
        return
    if not is_user_resolution(content):
        return
    _persist_closure(conn, ResolutionEntry(
        summary=_resolution_summary(content),
        topic_tokens=_collect_resolution_topics(chat_messages, message_id),
        message_id=message_id,
        created_at=created_at,
    ))


def sync_concern_closures(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> None:
    conn.execute('BEGIN IMMEDIATE')
    try:
        cursor = _get_sync_cursor(conn)
        if cursor == 0:
            messages = fetch_chat_messages(conn, lookback_hours=lookback_hours)
        else:
            messages = _fetch_incremental_chat_messages(conn, after_message_id=cursor)
        if not messages:
            conn.commit()
            return

        max_id = cursor
        bootstrap = cursor == 0
        for msg in messages:
            message_id = int(msg.get('id') or 0)
            if message_id <= cursor:
                continue
            if _is_user_author(msg.get('author')):
                context = (
                    messages
                    if bootstrap
                    else _fetch_messages_up_to(conn, message_id)
                )
                _apply_concern_event(conn, msg, context)
            max_id = max(max_id, message_id)

        if max_id > cursor:
            _set_sync_cursor(conn, max_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def build_resolution_state(
    user_messages: Sequence[dict],
    chat_messages: Sequence[dict] | None = None,
) -> ResolutionState:
    """Ephemeral builder for unit tests without DB."""
    chat_messages = list(chat_messages or user_messages)
    active: list[ResolutionEntry] = []
    for msg in user_messages:
        content = str(msg.get('content') or '')
        message_id = int(msg.get('id') or 0)
        if is_user_reopen(content):
            reopen_tokens = identity_topic_tokens(content)
            active = [
                entry for entry in active
                if not substantive_topic_overlap(entry.topic_tokens, reopen_tokens)
            ]
            continue
        if not is_user_resolution(content):
            continue
        active.append(ResolutionEntry(
            summary=_resolution_summary(content),
            topic_tokens=_collect_resolution_topics(chat_messages, message_id),
            message_id=message_id,
            created_at=str(msg.get('created_at') or ''),
        ))
    return ResolutionState(active=active)


def load_resolution_state(
    conn: sqlite3.Connection,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> ResolutionState:
    try:
        sync_concern_closures(conn, lookback_hours=lookback_hours)
    except sqlite3.OperationalError:
        pass
    except Exception:
        pass
    try:
        return load_active_closure_state(conn)
    except Exception:
        return ResolutionState(active=[])


def load_resolution_state_from_db(get_db_fn, *, lookback_hours: int = _DEFAULT_LOOKBACK_HOURS) -> ResolutionState:
    conn = get_db_fn()
    try:
        return load_resolution_state(conn, lookback_hours=lookback_hours)
    finally:
        conn.close()


def find_superseding_resolution(
    text: str,
    state: ResolutionState | None,
    *,
    recorded_at: str = '',
) -> ResolutionEntry | None:
    if not state or not state.active:
        return None
    tokens = identity_topic_tokens(text)
    if not tokens:
        return None
    for entry in state.active:
        if not substantive_topic_overlap(tokens, entry.topic_tokens):
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
) -> bool:
    return find_superseding_resolution(text, state, recorded_at=recorded_at) is not None


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
        if any(substantive_topic_overlap(entry.topic_tokens, kept.topic_tokens) for kept in picked):
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


def _append_suppressed_id(target: list[int], raw_id) -> None:
    try:
        value = int(raw_id)
    except (TypeError, ValueError):
        return
    if value > 0 and value not in target:
        target.append(value)


def merge_wake_consume_ids(visible_ids: Iterable, suppressed_ids: Iterable) -> list[int]:
    merged: list[int] = []
    for value in list(visible_ids or ()) + list(suppressed_ids or ()):
        _append_suppressed_id(merged, value)
    return merged


def filter_wake_rows(rows: Iterable, state: ResolutionState | None) -> FilterResult:
    kept = []
    applied: list[ResolutionEntry] = []
    suppressed_ids: list[int] = []
    for row in rows:
        content, recorded_at = _row_content_and_time(row)
        match = find_superseding_resolution(content, state, recorded_at=recorded_at)
        if match:
            applied.append(match)
            if hasattr(row, 'keys') and 'id' in row.keys():
                _append_suppressed_id(suppressed_ids, row['id'])
            elif isinstance(row, dict):
                _append_suppressed_id(suppressed_ids, row.get('id'))
            continue
        kept.append(row)
    return FilterResult(kept=kept, applied_resolutions=applied, suppressed_ids=suppressed_ids)


def filter_wake_items(items: Iterable[dict], state: ResolutionState | None) -> FilterResult:
    kept: list[dict] = []
    applied: list[ResolutionEntry] = []
    suppressed_ids: list[int] = []
    for item in items or ():
        content = str(item.get('content') or '')
        recorded_at = str(item.get('woke_at') or '')
        match = find_superseding_resolution(content, state, recorded_at=recorded_at)
        if match:
            applied.append(match)
            _append_suppressed_id(suppressed_ids, item.get('id'))
            continue
        kept.append(dict(item))
    return FilterResult(kept=kept, applied_resolutions=applied, suppressed_ids=suppressed_ids)


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
