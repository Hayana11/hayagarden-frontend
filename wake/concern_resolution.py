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

_STATE_TOKENS = frozenset({
    '完成', '解决', '恢复', '修好', '送到', '到账', '取到', '愈合', '好转',
    '取完', '跟完', '发货', '失败', '报错', '排查', '处理', '正常', '没事',
    '好了', '结束', '完了', '过去', '放下', '送达', '修复', '已完成', '未完成',
    '处理好', '处理完', '已修复', '未送到', '已送到',
})

_STATE_TOKEN_RE = re.compile(
    r'^(?:未|已|没|不|仍|还)?(?:'
    r'完成|解决|恢复|修好|修复|送到|送达|处理好|处理完|跟完|取完|取到|到账|'
    r'愈合|好转|好了|结束|完了|发货|排查|报错|失败'
    r')$',
)

_CLAUSE_SPLIT_RE = re.compile(r'[？?。！；\n]+')
_QUESTION_ONLY_FRAGMENT_RE = re.compile(
    r'^(?:怎么|如何|要不要|该不该|怎么办|怎么处理|要不要处理|需要吗|行吗).*$',
)
_COMMA_BEFORE_QUESTION_RE = re.compile(
    r'[，,]\s*(?=(?:怎么|如何|要不要|该不该|怎么办|怎么处理|要不要处理))',
)
_COMMA_STATE_TRANSITION_RE = re.compile(
    r'[，,]\s*(?=(?:后来|其实|不过|但|然而|刚才|已经|确认))',
)
_CONTRAST_CONNECTOR_RE = re.compile(
    r'(?:[，,]\s*|\s+|(?<=[\u4e00-\u9fff0-9a-z_-]))(但是|然而|不过|可是|但)',
)
_TRAILING_QUESTION_SUFFIX_RE = re.compile(
    r'(?:[，,]\s*)?(?:'
    r'怎么(?:办|处理)|要不要(?:去|继续|再|还)?[\u4e00-\u9fff]{0,4}|'
    r'该不该|如何[\u4e00-\u9fff]{0,6}|需要吗|行吗'
    r')[吗？?]*$',
)

_EVENT_PRIORITY = {'unresolved': 3, 'reopen': 2, 'resolve': 1}

_CLAUSE_LOCAL_SEP_RE = re.compile(r'[，,；;]|(?:但|但是|不过|然而|可是)')
_CONDITIONAL_BEFORE_REOPEN_RE = re.compile(
    r'(?:如果|要是|假如|万一|以后若|倘若|若是)',
)
_REOPEN_FAULT_NEGATED_RE = re.compile(
    r'(?:没有|并没|并没有|并未|不是|不会|没在|别再)(?:再|仍|还)?$',
)
_FACTUAL_CONTRAST_BREAK_RE = re.compile(r'(?:但|但是|不过|然而|可是)|今天真的|其实|真的')
_CONDITIONAL_PREFIX_RE = re.compile(r'^(?:如果|要是|假如|万一|以后若|倘若|若是)')
_FUTURE_WAIT_PREFIX_RE = re.compile(r'^(?:等|在等|想等)')
_WISH_PREFIX_RE = re.compile(
    r'^(?:希望|但愿|本想|本来想|本来希望)',
)
_SPEAKER_PREFIX_RE = r'(?:我们|咱们|我)'
_WISH_INTENT_RE = re.compile(
    rf'^{_SPEAKER_PREFIX_RE}?(?:只是)?(?:希望|但愿|本想|本来想|本来希望)',
)
_WAIT_INTENT_RE = re.compile(
    rf'^{_SPEAKER_PREFIX_RE}?(?:只是)?(?:等|在等|想等)',
)
_INTENT_WANT_RE = re.compile(
    rf'^{_SPEAKER_PREFIX_RE}?想(?!等)',
)
_OUTER_NEGATES_UNRESOLVED_RE = re.compile(
    r'^(?:'
    r'并不是说|并不是|'
    r'并非|'
    r'不是|'
    r'没有发现|并未发现|没发现'
    r')',
)
_NON_FACTUAL_NEGATION_SEGMENT_RE = re.compile(
    r'^(?:'
    r'并不?是|不是说|并不是说|'
    r'没有|并没|并没有|并未|没在|别再|不会|'
    r'未发现|没发现|没有发现|并未发现'
    r')',
)
_NON_FACTUAL_NEGATION_BEFORE_RE = re.compile(
    r'(?:'
    r'(?:没有|并没|并没有|并未|不是|并不会|不会|没在|别再|未发现|没发现|没有发现|并未发现)'
    r'(?:说|表示|提到|发现|看到)?'
    r'[^，,；;]{0,16}'
    r')$',
)

_NEGATIVE_POLARITY_RE = re.compile(
    r'(?:还没|仍未|还是|仍然|依然|未曾|没有|没|未|不)$',
)

_NEGATIVE_UNRESOLVED_MARKERS = tuple(
    re.compile(p)
    for p in (
        r'(?:还没|仍未|还是|仍然|依然|没|未|不)(?:有)?[^，,]{0,6}?(?:'
        r'完成|解决|处理好|处理完|送到|送达|修好|修复|取完|取到|到账|'
        r'恢复|愈合|好转|好了|结束'
        r')',
    )
)

_RESOLUTION_MARKERS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'(?:已经|已|后来|现在|刚|刚刚|确认|终于)?(?:好了|没事了|解决(?:了)?|处理(?:完|好)(?:了)?|取完|送到(?:了)?|到账|修好(?:了)?|恢复(?:了)?|完成(?:了)?|跟完|愈合(?:了)?|好转(?:了)?)',
        r'又.{0,8}(?:好了|修好|送到|取完|取到|到账|完成|解决|处理(?:完|好)|跟完|愈合)',
        r'(?:无需|不需|不用|不必|不要)(?:再|继续|进一步|跑|催|问|打|管|担心)',
        r'(?:已经|这件事).{0,16}可以放下',
        r'不用再(?:问|担心|追问|管|跑|催|打)',
        r'这件事(?:已经)?(?:结束|过去|完了)',
    )
)

_REOPEN_MARKERS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'又(?:出|坏|失败|报错|延迟|问题|恶化|反复|卡住|停|漏|找不到)',
        r'又.{0,8}(?:红|肿|疼|痛|发炎|渗|坏|错)',
        r'(?:还是|仍然|依然)(?:没|不|有)(?:好|行|完成|解决|取到|修好|到账|送到)',
        r'(?:又得|还是要|还是得|仍要|仍得|需要再|还得)',
        r'(?:重新|再次)(?:出现|发生|报错|出问题|问|处理|催|跑|取)',
        r'(?:今天|昨天|刚才|刚刚|现在).{0,12}(?:出问题|报错|失败|恶化|坏了|找不到|延迟|开始|又)',
        r'(?:开始|出现)(?:问题|故障|报错|状况|反复|了|着)',
        r'(?:仍|还)(?:有|没|未)[^，,]{0,8}(?:问题|故障|报错|取到|修好|到账|送到)',
        r'(?:还|仍)在.{0,6}(?:报错|出问题|有问题|故障|失败|恶化)',
        r'(?:又|仍|还)?[^，,]{0,4}(?:恶化|红肿|发炎|渗|出错)(?:了|着)?',
    )
)

_TRAILING_STATE_PHRASE_RE = re.compile(
    r'(?:已经|已|仍未|还没|未|不)?(?:'
    r'完成(?:了)?|解决(?:了)?|恢复(?:了)?|修好(?:了)?|修复(?:了)?|'
    r'送到(?:了)?|送达(?:了)?|处理好(?:了)?|处理完(?:了)?|跟完(?:了)?|'
    r'取完(?:了)?|取到(?:了)?|到账(?:了)?|愈合(?:了)?|好转(?:了)?|好了|'
    r'结束(?:了)?|完了|发货(?:了)?'
    r')$',
)
_LEADING_FILLER_RE = re.compile(
    r'^(?:醒来|还在想|我还在想|还在担心|还在|仍然|依然|医生|记得|提醒|想问|关于|她之前被|他之前被|我之前被|她被|他被|日记里|夜里仍惦记|这次说的是|说的是)+',
)
_TRAILING_WORRY_RE = re.compile(
    r'(?:还在疼|还在痛|仍没|仍未|仍然没|有没有取|有没取|要不要打|要不要|怎么办|怎么处理).*$',
)
_FILLER_CHUNK_RE = re.compile(
    r'^(?:不用再|无需|可以|关于|后来|之前|当时).*$',
)

_DOMAIN_GENERIC = frozenset({
    '进度', '订单', '工厂', '服务', '故障', '部署', '问题', '系统', '项目',
    '前端', '后端', '接口', '模块', '版本', '环境', '厂家', '产线',
})

_KNOWN_ENTITY_CORES = (
    '有骨有尾', '无骨有尾', '有骨无尾', '无骨无尾',
    '磁吸尾', '意向金', '尾款', '还款', '退款', '快递', '订单', '衬衫',
    '破伤风', '抓伤', '伤口', '红肿', '渗液',
)
_EMBEDDABLE_ENTITY_CORES = tuple(
    core for core in _KNOWN_ENTITY_CORES
    if core not in {'衬衫', '订单'}
)

_GENERIC_TOPIC_TOKENS = frozenset({
    '已经', '可以', '不用', '无需', '没有', '什么', '怎么', '我们', '你们', '自己',
    '一下', '继续', '还是', '就是', '这个', '那个', '事情', '事项', '今天', '昨天',
    '现在', '之后', '之前', '感觉', '知道', '觉得', '告诉', '她说', '他说', '我说',
    '问我', '问你', '好了', '没事', '结束', '放下', '再问', '这件事', '处理', '完了',
    '过去', '结案', '不用了', '没事了', '都好了', '进一步', '再跑', '再问', '担心',
    '有点', '还没', '已经取', '取完',
})

_NON_ENTITY_FILLER_TOKENS = frozenset({
    '如果', '要是', '假如', '万一', '倘若', '若是', '以后若',
    '今天真的', '今天确认', '其实', '说的是', '关于', '放心',
})

_DEFAULT_LOOKBACK_HOURS = 168
_GUARD_MAX_ENTRIES = 3
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS concern_closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    topic_tokens TEXT NOT NULL,
    source_message_id INTEGER,
    resolved_at TEXT NOT NULL,
    reopened_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_concern_closures_active
    ON concern_closures(active, resolved_at);
CREATE INDEX IF NOT EXISTS idx_concern_closures_source
    ON concern_closures(source_message_id);
CREATE TABLE IF NOT EXISTS concern_closure_sync (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_processed_message_id INTEGER NOT NULL DEFAULT 0
);
"""

_STOPWORDS = _GENERIC_TOPIC_TOKENS


@dataclass(frozen=True)
class ConcernClauseEvent:
    event: str
    topics: frozenset[str] = field(default_factory=frozenset)
    clause: str = ''


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
    return re.sub(r'[，。！？、；：\s]+', ' ', text).strip()


def is_state_token(token: str) -> bool:
    return token in _STATE_TOKENS or bool(_STATE_TOKEN_RE.fullmatch(token))


def _extract_entity_core(chunk: str) -> str:
    value = chunk.strip()
    if not value or is_state_token(value):
        return ''
    if _FILLER_CHUNK_RE.match(value):
        return ''
    value = re.sub(r'^(?:医生说|医生|说)+', '', value)
    value = re.sub(r'^(?:不用|无需|不必|不用再|可以)+', '', value)
    value = re.sub(r'^(?:猫抓的?|被猫抓的?)', '', value)
    value = _LEADING_FILLER_RE.sub('', value)
    value = re.sub(r'^(?:她|他|我)?(?:需不需要|要不要|有没有|有没)', '', value)
    value = re.sub(r'^(?:打|用)', '', value)
    value = re.sub(r'又.{0,8}找不到.*$', '', value)
    value = re.sub(r'又.{0,8}(?:红|肿|疼|痛|发炎|渗|坏|错|恶化|问题|故障|报错).*$', '', value)
    value = re.sub(
        r'(?:今天|昨天|刚才|刚刚|现在).*(?:开始|出现).*$',
        '',
        value,
    )
    value = _TRAILING_STATE_PHRASE_RE.sub('', value)
    value = re.sub(
        r'(?:早就|已经|已|仍未|还没|未|不)?(?:愈合|好了|结束|完成|解决|恢复|修好|送到|取完|到账|好转).*$',
        '',
        value,
    )
    value = re.sub(
        r'(?:的)?(?:进度|情况|状态)?(?:已经|已|仍未|还没|未).*$',
        '',
        value,
    )
    value = re.sub(r'的(?:进度|情况|状态)$', '', value)
    value = re.sub(r'(?<![\u4e00-\u9fff])要不要.*$', '', value)
    value = _TRAILING_WORRY_RE.sub('', value)
    return value.strip()


def _normalize_entity_variant(core: str) -> set[str]:
    variants: set[str] = {core}
    trimmed = re.sub(
        r'(?:要不要|有没有|有没|怎么办|怎么处理|仍让我放不下).*$',
        '',
        core,
    ).strip()
    if trimmed and trimmed != core:
        variants.add(trimmed)
        core = trimmed
    for known_core in sorted(_KNOWN_ENTITY_CORES, key=len, reverse=True):
        if core == known_core:
            variants.add(known_core)
            continue
        if core.endswith(known_core) and len(core) > len(known_core):
            prefix = core[:-len(known_core)]
            if len(prefix) <= 2 and not _has_entity_discriminator(prefix):
                variants.add(known_core)
        if core.startswith(known_core) and len(core) > len(known_core):
            suffix = core[len(known_core):]
            if _has_entity_discriminator(suffix):
                continue
            if len(suffix) <= 2:
                variants.add(known_core)
    for known_core in sorted(_EMBEDDABLE_ENTITY_CORES, key=len, reverse=True):
        start = core.find(known_core)
        if start < 0:
            continue
        prefix = core[:start]
        suffix = core[start + len(known_core):]
        if _has_entity_discriminator(suffix):
            continue
        if _suffix_preserves_distinct_entity(suffix):
            continue
        if len(prefix) <= 4 and len(suffix) <= 2:
            variants.add(known_core)
    return {item for item in variants if item and len(item) >= 2 and not is_state_token(item)}


def _has_entity_discriminator(suffix: str) -> bool:
    if not suffix:
        return False
    return bool(re.match(r'^[a-z0-9]', suffix))


def _suffix_preserves_distinct_entity(suffix: str) -> bool:
    if _has_entity_discriminator(suffix):
        return True
    return bool(re.match(r'^订单[a-z0-9]', suffix))


def _identity_cores_from_chunk(chunk: str) -> set[str]:
    cores: set[str] = set()
    core = _extract_entity_core(chunk)
    if core and len(core) >= 2 and not is_state_token(core):
        cores.update(_normalize_entity_variant(core))
    for known_core in sorted(_EMBEDDABLE_ENTITY_CORES, key=len, reverse=True):
        start = chunk.find(known_core)
        if start < 0:
            continue
        prefix = chunk[:start]
        suffix = chunk[start + len(known_core):]
        if _has_entity_discriminator(suffix):
            continue
        if _suffix_preserves_distinct_entity(suffix):
            continue
        if len(prefix) <= 8 and len(suffix) <= 4:
            cores.add(known_core)
    return cores


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
        tokens.update(_identity_cores_from_chunk(chunk))
    return frozenset(tokens)


def identity_topic_tokens(text: str) -> frozenset[str]:
    return frozenset(
        token for token in topic_tokens(text)
        if not is_state_token(token)
    )


def _identity_tokens(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(
        token for token in tokens
        if len(token) >= 2
        and token not in _GENERIC_TOPIC_TOKENS
        and token not in _DOMAIN_GENERIC
        and not is_state_token(token)
    )


def _matchable_tokens(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(
        token for token in tokens
        if len(token) >= 2
        and token not in _GENERIC_TOPIC_TOKENS
        and not is_state_token(token)
    )


def _token_prefix_suffix_embedded(shorter: str, longer: str) -> bool:
    return longer.startswith(shorter) or longer.endswith(shorter)


def _anchor_token_match(left_token: str, right_token: str) -> bool:
    if left_token in _DOMAIN_GENERIC or right_token in _DOMAIN_GENERIC:
        return False
    if is_state_token(left_token) or is_state_token(right_token):
        return False
    if left_token == right_token:
        return True
    shorter, longer = (left_token, right_token) if len(left_token) <= len(right_token) else (right_token, left_token)
    if len(shorter) < 2 or not _token_prefix_suffix_embedded(shorter, longer):
        return False
    if re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', shorter) and '-' not in shorter and '-' in longer:
        return False
    return True


def _exact_anchor_overlap(left: frozenset[str], right: frozenset[str]) -> bool:
    shared = (left & right) - _GENERIC_TOPIC_TOKENS - _DOMAIN_GENERIC
    for token in shared:
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', token):
            return True
        left_extended = any(item.startswith(f'{token}-') for item in left)
        right_extended = any(item.startswith(f'{token}-') for item in right)
        if left_extended == right_extended:
            return True
    return False


def substantive_topic_overlap(left: frozenset[str], right: frozenset[str]) -> bool:
    if _exact_anchor_overlap(left, right):
        return True
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
_PRONOUN_CONTEXT_RE = re.compile(r'(?:它|这个|那个)(?:又|还|仍)')


def _needs_immediate_prior_chat(content: str) -> bool:
    body = (content or '').strip()
    if _DEICTIC_PHRASE.search(body):
        return True
    if _PRONOUN_CONTEXT_RE.search(body):
        return True
    return False


def _immediate_prior_message_topics(
    chat_messages: Sequence[dict],
    message_id: int | None,
) -> frozenset[str]:
    if message_id is None:
        return frozenset()
    index = next(
        (i for i, msg in enumerate(chat_messages) if int(msg.get('id') or 0) == int(message_id)),
        -1,
    )
    if index <= 0:
        return frozenset()
    msg = chat_messages[index - 1]
    content = str(msg.get('content') or '')
    if not content.strip():
        return frozenset()
    prior_id = int(msg.get('id') or 0)
    sub_context = list(chat_messages[:index])
    events = parse_user_concern_events(content, sub_context, prior_id)
    for event in reversed(events):
        if event.topics:
            return event.topics
    if _is_user_author(msg.get('author')):
        entity_topics = _clause_explicit_entity_topics(content)
        if entity_topics and not _message_blocks_entity_topic_fallback(content):
            return entity_topics
    return identity_topic_tokens(content)


def _split_factual_question_suffix(clause: str) -> tuple[str, str]:
    trimmed = clause.strip()
    match = _TRAILING_QUESTION_SUFFIX_RE.search(trimmed)
    if not match:
        return trimmed, ''
    factual = trimmed[:match.start()].strip().rstrip('，,')
    if factual:
        return factual, trimmed[match.start():].strip()
    return trimmed, ''


def _distinctive_clause_identities(clause: str) -> frozenset[str]:
    tokens = _identity_tokens(identity_topic_tokens(clause))
    return frozenset(
        token for token in tokens
        if token not in {'今天', '昨天', '刚才', '现在', '这件事', '那件事'}
        and token not in _NON_ENTITY_FILLER_TOKENS
    )


def _contrast_split_concern_clauses(part: str) -> list[str]:
    part = part.strip()
    if not part:
        return []
    matches = list(_CONTRAST_CONNECTOR_RE.finditer(part))
    if not matches:
        return [part]
    segments: list[str] = []
    last_end = 0
    for match in matches:
        left = part[last_end:match.start()].strip()
        if left:
            segments.append(left)
        last_end = match.end()
    tail = part[last_end:].strip()
    if tail:
        segments.append(tail)
    if len(segments) < 2:
        return [part]
    events = [_classify_factual_clause(segment) for segment in segments]
    if not any(events):
        return [part]
    non_factual_segments = [segment for segment, event in zip(segments, events) if not event]
    if non_factual_segments and all(
        _clause_is_non_factual_fragment(segment) for segment in non_factual_segments
    ):
        return segments
    if all(events):
        identities = [_distinctive_clause_identities(segment) for segment in segments]
        if all(identities) and len({frozenset(identity) for identity in identities}) > 1:
            return segments
    return [part]


def _comma_split_concern_clauses(part: str) -> list[str]:
    pieces = [piece.strip() for piece in re.split(r'[，,]\s*', part) if piece.strip()]
    if len(pieces) < 2:
        return [part]
    events = [_classify_factual_clause(piece) for piece in pieces]
    if not any(events):
        return [part]
    non_factual_pieces = [piece for piece, event in zip(pieces, events) if not event]
    if non_factual_pieces and all(
        _clause_is_non_factual_fragment(piece) for piece in non_factual_pieces
    ):
        return pieces
    if all(events):
        identities = [_distinctive_clause_identities(piece) for piece in pieces]
        if all(identities) and len({frozenset(identity) for identity in identities}) > 1:
            return pieces
    return [part]


def _split_segment_clauses(segment: str, *, interrogative: bool) -> list[tuple[str, bool]]:
    clauses: list[tuple[str, bool]] = []
    contrast_parts = _contrast_split_concern_clauses(segment.strip())
    for contrast_idx, contrast_part in enumerate(contrast_parts):
        parts = _COMMA_BEFORE_QUESTION_RE.split(contrast_part.strip())
        for idx, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            subparts = _COMMA_STATE_TRANSITION_RE.split(part)
            if len(subparts) == 1:
                subparts = _comma_split_concern_clauses(part)
            for sub_idx, subpart in enumerate(subparts):
                subpart = subpart.strip()
                if not subpart:
                    continue
                is_last = (
                    contrast_idx == len(contrast_parts) - 1
                    and idx == len(parts) - 1
                    and sub_idx == len(subparts) - 1
                )
                clauses.append((subpart, interrogative and is_last))
    return clauses


def _split_concern_clauses(body: str) -> list[tuple[str, bool]]:
    clauses: list[tuple[str, bool]] = []
    segment = ''
    for ch in body:
        if ch in '？?。！；\n':
            was_question = ch in '？?'
            if segment.strip():
                clauses.extend(_split_segment_clauses(segment, interrogative=was_question))
            segment = ''
        else:
            segment += ch
    if segment.strip():
        clauses.extend(_split_segment_clauses(segment, interrogative=False))
    return clauses


def _factuality_subject(clause: str) -> str:
    subject = clause.strip()
    subject = re.sub(rf'^{_SPEAKER_PREFIX_RE}(?:只是)?', '', subject).strip()
    return subject


def _clause_has_wish_or_wait_intent(clause: str) -> bool:
    stripped = clause.strip()
    if (
        _WISH_INTENT_RE.match(stripped)
        or _WAIT_INTENT_RE.match(stripped)
        or _INTENT_WANT_RE.match(stripped)
    ):
        return True
    subject = _factuality_subject(clause)
    return bool(
        _WISH_PREFIX_RE.match(subject)
        or _FUTURE_WAIT_PREFIX_RE.match(subject)
    )


def _negative_state_assertion_is_outer_negated(clause: str, match: re.Match[str]) -> bool:
    local_start = _local_segment_start(clause, match.start())
    segment = clause[local_start:match.end()].strip()
    outer = _OUTER_NEGATES_UNRESOLVED_RE.match(segment)
    if not outer:
        return False
    tail = segment[outer.end():].strip()
    tail = re.sub(r'^(?:它|这|那)', '', tail).strip()
    if any(pattern.search(tail) for pattern in _NEGATIVE_UNRESOLVED_MARKERS):
        return True
    before = segment[:match.start() - local_start]
    return bool(_NEGATIVE_POLARITY_RE.search(before))


def _unresolved_match_is_outer_negated(clause: str, match: re.Match[str]) -> bool:
    return _negative_state_assertion_is_outer_negated(clause, match)


def _clause_has_outer_negated_unresolved(clause: str) -> bool:
    for pattern in _NEGATIVE_UNRESOLVED_MARKERS:
        for match in pattern.finditer(clause):
            if _unresolved_match_is_outer_negated(clause, match):
                return True
    return False


def _local_segment_start(clause: str, pos: int) -> int:
    start = 0
    for match in _CLAUSE_LOCAL_SEP_RE.finditer(clause[:pos]):
        start = match.end()
    return start


def _state_match_is_non_factual(
    clause: str,
    match: re.Match[str],
    *,
    event_kind: str,
) -> bool:
    local_start = _local_segment_start(clause, match.start())
    before = clause[local_start:match.start()]
    segment = clause[local_start:match.end()]
    segment_head = _factuality_subject(segment)

    conditional = _CONDITIONAL_BEFORE_REOPEN_RE.search(segment)
    if conditional:
        between = segment[conditional.end():match.start() - local_start]
        if not _FACTUAL_CONTRAST_BREAK_RE.search(between):
            return True
    if _CONDITIONAL_BEFORE_REOPEN_RE.search(before):
        if not _FACTUAL_CONTRAST_BREAK_RE.search(before):
            return True
    if _clause_has_wish_or_wait_intent(segment.strip()):
        return True
    if _CONDITIONAL_PREFIX_RE.match(segment_head):
        return True
    if re.match(r'^(?:如果|要是|假如|万一|以后若|倘若|若是)', segment_head):
        return True

    if event_kind == 'unresolved':
        if _unresolved_match_is_outer_negated(clause, match):
            return True
        return False

    if _NON_FACTUAL_NEGATION_SEGMENT_RE.match(segment_head):
        return True
    if _NON_FACTUAL_NEGATION_BEFORE_RE.search(before):
        return True
    if _REOPEN_FAULT_NEGATED_RE.search(before):
        return True
    if re.search(r'放心[^，,]{0,8}不会', before):
        return True
    return False


def _clause_blocks_resolution(clause: str) -> bool:
    return bool(_RESOLUTION_NEGATION.search(clause) or _RESOLUTION_RHETORICAL.search(clause))


def _match_has_negative_polarity(clause: str, match: re.Match[str]) -> bool:
    window = clause[max(0, match.start() - 8):match.start()]
    if '，' in window or ',' in window:
        window = re.split(r'[，,]', window)[-1]
    return bool(_NEGATIVE_POLARITY_RE.search(window))


def _pick_clause_event(events: list[tuple[int, int, str]]) -> str | None:
    if not events:
        return None
    winners: dict[int, tuple[int, str]] = {}
    for end, priority, event_type in events:
        current = winners.get(end)
        if current is None or priority > current[0]:
            winners[end] = (priority, event_type)
    last_end = max(winners)
    return winners[last_end][1]


def _collect_clause_state_events(clause: str) -> list[tuple[int, int, str]]:
    events: list[tuple[int, int, str]] = []
    for pattern in _NEGATIVE_UNRESOLVED_MARKERS:
        for match in pattern.finditer(clause):
            if _state_match_is_non_factual(clause, match, event_kind='unresolved'):
                continue
            events.append((match.end(), _EVENT_PRIORITY['unresolved'], 'unresolved'))
    for pattern in _REOPEN_MARKERS:
        for match in pattern.finditer(clause):
            if _state_match_is_non_factual(clause, match, event_kind='reopen'):
                continue
            events.append((match.end(), _EVENT_PRIORITY['reopen'], 'reopen'))
    if not _clause_blocks_resolution(clause):
        for pattern in _RESOLUTION_MARKERS:
            for match in pattern.finditer(clause):
                if _state_match_is_non_factual(clause, match, event_kind='resolve'):
                    continue
                if _match_has_negative_polarity(clause, match):
                    if _negative_state_assertion_is_outer_negated(clause, match):
                        continue
                    events.append((
                        match.end(),
                        _EVENT_PRIORITY['unresolved'],
                        'unresolved',
                    ))
                else:
                    events.append((
                        match.end(),
                        _EVENT_PRIORITY['resolve'],
                        'resolve',
                    ))
    return events


def _is_interrogative_about_state(clause: str) -> bool:
    trimmed = clause.strip().rstrip('。.!！… ')
    if not re.search(r'[吗？?]$', trimmed):
        return False
    stripped = re.sub(r'[吗？?]+$', '', trimmed).strip()
    if not stripped:
        return True
    if re.search(r'(?:已经|已).*(?:好了|修好|解决|完成|没事了|愈合|好转)', stripped):
        return True
    if re.search(r'(?:不用|无需|要不要|是否需要|需要吗)', stripped):
        return True
    return trimmed.endswith('吗')


def _clause_is_pure_question(clause: str, *, interrogative: bool = False) -> bool:
    factual, suffix = _split_factual_question_suffix(clause)
    if suffix and factual:
        return _clause_is_pure_question(factual, interrogative=False)
    trimmed = clause.strip().rstrip('。.!！… ')
    if not trimmed:
        return True
    if interrogative:
        return True
    if _QUESTION_ONLY_FRAGMENT_RE.fullmatch(trimmed):
        return True
    if _is_interrogative_about_state(trimmed):
        return True
    if re.fullmatch(r'(?:后来|之前|当时).*(?:怎么|如何).*(?:说|讲|处理)', trimmed):
        return True
    return False


def _classify_factual_clause(clause: str) -> str | None:
    return _pick_clause_event(_collect_clause_state_events(clause))


def _prior_chat_content(chat_messages: Sequence[dict], message_id: int | None) -> str:
    if message_id is None:
        return ''
    index = next(
        (i for i, msg in enumerate(chat_messages) if int(msg.get('id') or 0) == int(message_id)),
        -1,
    )
    if index > 0:
        return str(chat_messages[index - 1].get('content') or '')
    return ''


def _marker_event_kind(pattern: re.Pattern[str]) -> str:
    if pattern in _NEGATIVE_UNRESOLVED_MARKERS:
        return 'unresolved'
    if pattern in _REOPEN_MARKERS:
        return 'reopen'
    return 'resolve'


def _message_blocks_entity_topic_fallback(content: str) -> bool:
    body = (content or '').strip()
    if not body:
        return True
    saw_marker = False
    for clause, _ in _split_concern_clauses(body):
        factual, suffix = _split_factual_question_suffix(clause)
        target = factual if suffix and factual else clause
        if _clause_is_pure_question(target):
            continue
        for pattern in (*_REOPEN_MARKERS, *_NEGATIVE_UNRESOLVED_MARKERS, *_RESOLUTION_MARKERS):
            for match in pattern.finditer(target):
                saw_marker = True
                if not _state_match_is_non_factual(
                    target,
                    match,
                    event_kind=_marker_event_kind(pattern),
                ):
                    return False
    return saw_marker


def _collect_prior_chat_topics(
    chat_messages: Sequence[dict],
    message_id: int | None,
) -> frozenset[str]:
    if message_id is None:
        return frozenset()
    index = next(
        (i for i, msg in enumerate(chat_messages) if int(msg.get('id') or 0) == int(message_id)),
        -1,
    )
    for i in range(index - 1, -1, -1):
        msg = chat_messages[i]
        author = str(msg.get('author') or '').strip().lower()
        if author and author not in _USER_AUTHORS:
            continue
        content = str(msg.get('content') or '')
        if not content.strip():
            continue
        prior_id = int(msg.get('id') or 0)
        sub_context = list(chat_messages[:i + 1])
        events = parse_user_concern_events(content, sub_context, prior_id)
        for event in reversed(events):
            if event.topics:
                return event.topics
        entity_topics = _clause_explicit_entity_topics(content)
        if entity_topics and not _message_blocks_entity_topic_fallback(content):
            return entity_topics
    return frozenset()


def _clause_entity_anchors(clause: str) -> frozenset[str]:
    raw = identity_topic_tokens(clause)
    return frozenset(
        token for token in raw
        if len(token) >= 2
        and token not in _GENERIC_TOPIC_TOKENS
        and token not in _NON_ENTITY_FILLER_TOKENS
        and not is_state_token(token)
    )


def _clause_has_explicit_entity(clause: str) -> bool:
    return bool(_clause_explicit_entity_topics(clause))


def _clause_explicit_entity_topics(clause: str) -> frozenset[str]:
    working = clause.strip()
    conditional_tail = re.split(r'[，,](?=(?:如果|要是|假如|万一|倘若|若是|以后若))', working, maxsplit=1)
    if len(conditional_tail) > 1:
        working = conditional_tail[0].strip()
    working = re.sub(
        r'^(?:但|但是|不过|然而|可是|今天真的|今天确认|其实|本来担心|后来)',
        '',
        working,
    ).strip()
    if re.match(r'^只是', working):
        return frozenset()
    distinctive = _distinctive_clause_identities(working)
    if distinctive:
        return frozenset(distinctive)
    if _PRONOUN_CONTEXT_RE.search(clause) or _DEICTIC_PHRASE.search(clause):
        return frozenset()
    return _clause_entity_anchors(working)


def _clause_is_non_factual_fragment(clause: str) -> bool:
    if _clause_has_wish_or_wait_intent(clause):
        return True
    subject = _factuality_subject(clause)
    if (
        _CONDITIONAL_PREFIX_RE.match(subject)
        or re.match(r'^(?:如果|要是|假如|万一|以后若|倘若|若是)', subject)
    ):
        return True
    return _clause_has_outer_negated_unresolved(clause)


def _topics_from_prior_clauses(prior_clauses: Sequence[str]) -> frozenset[str]:
    for prior in reversed(prior_clauses):
        if _clause_is_non_factual_fragment(prior):
            continue
        topics = _clause_explicit_entity_topics(prior)
        if topics:
            return topics
    return frozenset()


def collect_clause_event_topics(
    clause: str,
    chat_messages: Sequence[dict],
    message_id: int | None = None,
    *,
    prior_clauses_in_message: Sequence[str] = (),
) -> frozenset[str]:
    own_topics = _clause_explicit_entity_topics(clause)
    if own_topics:
        return own_topics
    same_message_topics = _topics_from_prior_clauses(prior_clauses_in_message)
    if same_message_topics:
        return same_message_topics
    if _needs_immediate_prior_chat(clause):
        immediate_topics = _immediate_prior_message_topics(chat_messages, message_id)
        if immediate_topics:
            return immediate_topics
    prior_topics = _collect_prior_chat_topics(chat_messages, message_id)
    if prior_topics:
        return prior_topics
    return frozenset()


def parse_user_concern_events(
    text: str,
    chat_messages: Sequence[dict] | None = None,
    message_id: int | None = None,
) -> list[ConcernClauseEvent]:
    body = (text or '').strip()
    if not body:
        return []
    chat_messages = list(chat_messages or ())
    parsed: list[ConcernClauseEvent] = []
    prior_targets: list[str] = []
    for clause, interrogative in _split_concern_clauses(body):
        factual, suffix = _split_factual_question_suffix(clause)
        target = factual if suffix and factual else clause
        pure_interrogative = interrogative and not suffix
        if _clause_is_pure_question(target, interrogative=pure_interrogative):
            continue
        event_type = _classify_factual_clause(target)
        if event_type:
            parsed.append(ConcernClauseEvent(
                event=event_type,
                topics=collect_clause_event_topics(
                    target,
                    chat_messages,
                    message_id,
                    prior_clauses_in_message=prior_targets,
                ),
                clause=target,
            ))
        if _clause_has_explicit_entity(target):
            prior_targets.append(target)
    return parsed


def classify_user_concern_event(text: str) -> str | None:
    body = (text or '').strip()
    if not body:
        return None
    last_event: str | None = None
    for clause, interrogative in _split_concern_clauses(body):
        factual, suffix = _split_factual_question_suffix(clause)
        target = factual if suffix and factual else clause
        pure_interrogative = interrogative and not suffix
        if _clause_is_pure_question(target, interrogative=pure_interrogative):
            continue
        event = _classify_factual_clause(target)
        if event:
            last_event = event
    return last_event


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


def collect_event_topics(chat_messages: Sequence[dict], message_id: int) -> frozenset[str]:
    index = next(
        (i for i, msg in enumerate(chat_messages) if int(msg.get('id') or 0) == int(message_id)),
        -1,
    )
    if index < 0:
        return frozenset()
    content = str(chat_messages[index].get('content') or '')
    return collect_clause_event_topics(content, chat_messages, message_id)


def _collect_resolution_topics(chat_messages: Sequence[dict], message_id: int) -> frozenset[str]:
    return collect_event_topics(chat_messages, message_id)


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
        if _deactivate_topic_overlap(tokens, reopen_tokens):
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
        existing_rows = conn.execute(
            "SELECT id, topic_tokens FROM concern_closures WHERE source_message_id=?",
            (int(entry.message_id),),
        ).fetchall()
        for row in existing_rows:
            try:
                tokens = frozenset(json.loads(row['topic_tokens'] or '[]'))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if substantive_topic_overlap(entry.topic_tokens, tokens):
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


def _deactivate_topic_overlap(stored: frozenset[str], event_topics: frozenset[str]) -> bool:
    if substantive_topic_overlap(stored, event_topics):
        return True
    shared = (stored & event_topics) - _GENERIC_TOPIC_TOKENS
    if not shared:
        return False
    stored_extra = _identity_tokens(stored) - shared
    event_extra = _identity_tokens(event_topics) - shared
    if stored_extra or event_extra:
        return False
    return True


def _apply_concern_events_to_active(
    active: list[ResolutionEntry],
    clause_events: Sequence[ConcernClauseEvent],
    *,
    content: str,
    message_id: int,
    created_at: str,
) -> list[ResolutionEntry]:
    for clause_event in clause_events:
        if clause_event.event == 'resolve':
            if not clause_event.topics:
                continue
            active.append(ResolutionEntry(
                summary=_resolution_summary(clause_event.clause or content),
                topic_tokens=clause_event.topics,
                message_id=message_id,
                created_at=created_at,
            ))
            continue
        if clause_event.event in {'reopen', 'unresolved'}:
            active = [
                entry for entry in active
                if not _deactivate_topic_overlap(entry.topic_tokens, clause_event.topics)
            ]
    return active


def _apply_concern_event(
    conn: sqlite3.Connection,
    msg: dict,
    chat_messages: Sequence[dict],
) -> None:
    content = str(msg.get('content') or '')
    created_at = str(msg.get('created_at') or '')
    message_id = int(msg.get('id') or 0)
    for clause_event in parse_user_concern_events(content, chat_messages, message_id):
        if clause_event.event == 'resolve':
            if not clause_event.topics:
                continue
            _persist_closure(conn, ResolutionEntry(
                summary=_resolution_summary(clause_event.clause or content),
                topic_tokens=clause_event.topics,
                message_id=message_id,
                created_at=created_at,
            ))
            continue
        if clause_event.event in {'reopen', 'unresolved'}:
            _deactivate_matching_closures(
                conn,
                clause_event.topics,
                reopened_at=created_at,
            )


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
        clause_events = parse_user_concern_events(content, chat_messages, message_id)
        active = _apply_concern_events_to_active(
            active,
            clause_events,
            content=content,
            message_id=message_id,
            created_at=str(msg.get('created_at') or ''),
        )
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
