"""Reality Context — time / elapsed / daily weather anchors for Chat turns.

P-CONTEXT-LEAN-H1A. Facts only. No Forge serializer / Capacity Swap / DB schema.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from chat.daily_context import (
    DEFAULT_TIMEZONE,
    _USER_AUTHORS,
    _table_columns,
    _wake_content_set,
    get_meta_int,
    is_formal_chat_message,
    META_SOURCE_KIND_CUTOVER,
)
from chat import daily_context as dc
from chat.weather_authority import (
    WeatherFetcher,
    WeatherSnapshot,
    try_fetch_weather_now,
)

_SHANGHAI = ZoneInfo(DEFAULT_TIMEZONE)
_ELAPSED_THRESHOLD = timedelta(minutes=15)

REASON_NEW_CONTEXT = 'new_context'
REASON_CROSS_DAY = 'cross_day'
REASON_ELAPSED_GT_15M = 'elapsed_gt_15m'
REASON_NONE = 'none'

WEATHER_REASON_FIRST_USER = 'first_user_turn_of_natural_day'
WEATHER_REASON_NONE = 'none'

_CREATED_AT_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?'
)


@dataclass(frozen=True)
class RealityContextResult:
    time_anchor: str
    weather_anchor: str
    time_anchor_reason: Optional[str]
    weather_anchor_reason: Optional[str]
    weather_status: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            'time_anchor': self.time_anchor,
            'weather_anchor': self.weather_anchor,
            'time_anchor_reason': self.time_anchor_reason,
            'weather_anchor_reason': self.weather_anchor_reason,
            'weather_status': self.weather_status,
        }

    def provider_prefix(self) -> str:
        parts = [p for p in (self.time_anchor, self.weather_anchor) if str(p or '').strip()]
        return '\n\n'.join(parts)


def shanghai_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(_SHANGHAI)
    if now.tzinfo is None:
        return now.replace(tzinfo=_SHANGHAI)
    return now.astimezone(_SHANGHAI)


def natural_calendar_day(dt: datetime) -> str:
    """Natural calendar day in Asia/Shanghai — not Daily Soft Window 04:00 boundary."""
    return shanghai_now(dt).strftime('%Y-%m-%d')


def parse_message_created_at(raw: Any) -> Optional[datetime]:
    text = str(raw or '').strip()
    if not text:
        return None
    match = _CREATED_AT_RE.match(text)
    if not match:
        return None
    day, hh, mm, ss = match.group(1), match.group(2), match.group(3), match.group(4) or '00'
    try:
        return datetime(
            int(day[0:4]), int(day[5:7]), int(day[8:10]),
            int(hh), int(mm), int(ss),
            tzinfo=_SHANGHAI,
        )
    except ValueError:
        return None


def _format_wall(dt: datetime) -> str:
    return shanghai_now(dt).strftime('%Y-%m-%d %H:%M')


def _format_elapsed(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, _ = divmod(rem, 60)
    if hours > 0:
        return f'{hours}小时{minutes}分钟'
    return f'{minutes}分钟'


def _date_relation(prev_day: str, cur_day: str) -> str:
    if prev_day == cur_day:
        return 'same_day'
    try:
        prev = datetime.strptime(prev_day, '%Y-%m-%d').date()
        cur = datetime.strptime(cur_day, '%Y-%m-%d').date()
    except ValueError:
        return 'older'
    if (cur - prev).days == 1:
        return 'previous_day'
    return 'older'


def _date_relation_zh(rel: str) -> str:
    if rel == 'same_day':
        return '上一条发生在今天'
    if rel == 'previous_day':
        return '上一条发生在昨天'
    return '上一条发生在更早的日期'


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    return dc._connect(db_path)


def _load_message_row(
    conn: sqlite3.Connection,
    message_id: int,
) -> Optional[Mapping[str, Any]]:
    cols = _table_columns(conn, 'chat_messages')
    if not cols or 'id' not in cols:
        return None
    select_cols = ['id', 'author', 'content', 'created_at']
    for optional in ('tool_calls', 'source_kind', 'image_url'):
        if optional in cols:
            select_cols.append(optional)
    row = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM chat_messages WHERE id=?",
        (int(message_id),),
    ).fetchone()
    return row


def find_previous_formal_user_message(
    *,
    before_message_id: int,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict[str, Any]]:
    """Latest formal user chat message strictly before ``before_message_id``.

    Wake / workspace_job / system / tool rows never qualify.
    """
    own = conn is None
    c = conn or _connect(db_path)
    try:
        cols = _table_columns(c, 'chat_messages')
        if not cols:
            return None
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url'):
            if optional in cols:
                select_cols.append(optional)
        authors = sorted(_USER_AUTHORS)
        placeholders = ','.join('?' for _ in authors)
        rows = c.execute(
            f'''SELECT {', '.join(select_cols)} FROM chat_messages
                WHERE id < ? AND lower(author) IN ({placeholders})
                ORDER BY id DESC LIMIT 64''',
            (int(before_message_id), *authors),
        ).fetchall()
        wake_contents = _wake_content_set(c)
        cutover = get_meta_int(c, META_SOURCE_KIND_CUTOVER)
        for row in rows:
            if not is_formal_chat_message(
                row, wake_contents=wake_contents, cutover_id=cutover,
            ):
                continue
            created = parse_message_created_at(row['created_at'])
            return {
                'id': int(row['id']),
                'created_at': created,
                'created_at_raw': str(row['created_at'] or ''),
            }
        return None
    finally:
        if own:
            c.close()


def is_first_formal_user_of_natural_day(
    *,
    current_user_message_id: int,
    now: Optional[datetime] = None,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """True when no earlier formal user exists on the same natural calendar day."""
    own = conn is None
    c = conn or _connect(db_path)
    try:
        current = _load_message_row(c, int(current_user_message_id))
        if current is None:
            return False
        cur_dt = parse_message_created_at(current['created_at']) or shanghai_now(now)
        day = natural_calendar_day(cur_dt)
        day_start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=_SHANGHAI)
        day_end = day_start + timedelta(days=1)
        start_s = day_start.strftime('%Y-%m-%d %H:%M:%S')
        end_s = day_end.strftime('%Y-%m-%d %H:%M:%S')

        cols = _table_columns(c, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url'):
            if optional in cols:
                select_cols.append(optional)
        authors = sorted(_USER_AUTHORS)
        placeholders = ','.join('?' for _ in authors)
        rows = c.execute(
            f'''SELECT {', '.join(select_cols)} FROM chat_messages
                WHERE id < ? AND lower(author) IN ({placeholders})
                  AND created_at >= ? AND created_at < ?
                ORDER BY id ASC''',
            (int(current_user_message_id), *authors, start_s, end_s),
        ).fetchall()
        wake_contents = _wake_content_set(c)
        cutover = get_meta_int(c, META_SOURCE_KIND_CUTOVER)
        for row in rows:
            if is_formal_chat_message(
                row, wake_contents=wake_contents, cutover_id=cutover,
            ):
                return False
        return True
    finally:
        if own:
            c.close()


def _choose_time_reason(
    *,
    is_new_model_context: bool,
    cross_day: bool,
    elapsed_gt_15m: bool,
) -> str:
    # Priority: cross_day > new_context > elapsed_gt_15m
    if cross_day:
        return REASON_CROSS_DAY
    if is_new_model_context:
        return REASON_NEW_CONTEXT
    if elapsed_gt_15m:
        return REASON_ELAPSED_GT_15M
    return REASON_NONE


def _build_time_anchor_text(
    *,
    now: datetime,
    prev: Optional[dict[str, Any]],
    relation: Optional[str],
) -> str:
    lines = [
        '【现实时间锚】',
        f'现在：{_format_wall(now)}（{DEFAULT_TIMEZONE}）',
    ]
    if prev is None or prev.get('created_at') is None:
        lines.append('上一条用户消息：无')
        lines.append('这是现实时间事实，不要求主动复述；不得据此推断用户期间去做了什么。')
        return '\n'.join(lines)

    prev_dt: datetime = prev['created_at']
    elapsed = shanghai_now(now) - prev_dt
    lines.append(f'上一条用户消息：{_format_wall(prev_dt)}')
    lines.append(f'间隔：{_format_elapsed(elapsed)}')
    if relation:
        lines.append(f'日期关系：{_date_relation_zh(relation)}')
    lines.append('这是现实时间事实，不要求主动复述；不得据此推断用户期间去做了什么。')
    return '\n'.join(lines)


def _build_weather_anchor_text(snap: WeatherSnapshot) -> str:
    return '\n'.join([
        '【今日天气】',
        f'地点：{snap.location}',
        f'当前：{snap.temperature_c}°C，{snap.weather_text}',
        f'湿度：{snap.humidity_pct}%',
        f'数据时间：{snap.observed_at}',
        '',
        '这是现实环境事实，不要求主动复述；仅在与当前对话自然相关时使用。',
    ])


def build_reality_context(
    *,
    current_user_message_id: int,
    is_new_model_context: bool = False,
    now: Optional[datetime] = None,
    db_path: Optional[str] = None,
    weather_fetcher: Optional[WeatherFetcher] = None,
    include_weather: bool = True,
) -> dict[str, Any]:
    """Unified Reality Context builder for Daily + Manual Forge first-turn.

    Returns dict with time_anchor / weather_anchor / reasons. Empty strings when
    no injection is warranted. Weather failures yield empty weather_anchor
    (never mock 24°C/62%/多云).
    """
    dc.ensure_schema(db_path)
    wall = shanghai_now(now)
    mid = int(current_user_message_id)

    prev = find_previous_formal_user_message(
        before_message_id=mid, db_path=db_path,
    )
    cross_day = False
    elapsed_gt_15m = False
    relation: Optional[str] = None
    if prev is not None and prev.get('created_at') is not None:
        prev_day = natural_calendar_day(prev['created_at'])
        cur_day = natural_calendar_day(wall)
        relation = _date_relation(prev_day, cur_day)
        cross_day = relation != 'same_day'
        elapsed_gt_15m = (wall - prev['created_at']) > _ELAPSED_THRESHOLD

    reason = _choose_time_reason(
        is_new_model_context=bool(is_new_model_context),
        cross_day=cross_day,
        elapsed_gt_15m=elapsed_gt_15m,
    )

    time_anchor = ''
    if reason != REASON_NONE:
        time_anchor = _build_time_anchor_text(now=wall, prev=prev, relation=relation)

    weather_anchor = ''
    weather_reason: Optional[str] = WEATHER_REASON_NONE
    weather_status: Optional[str] = None
    if include_weather:
        first = is_first_formal_user_of_natural_day(
            current_user_message_id=mid, now=wall, db_path=db_path,
        )
        if first:
            weather_reason = WEATHER_REASON_FIRST_USER
            snap = try_fetch_weather_now(now=wall, fetcher=weather_fetcher)
            if snap is None:
                weather_status = 'unavailable'
                weather_anchor = ''
            else:
                weather_status = 'ok'
                weather_anchor = _build_weather_anchor_text(snap)
        else:
            weather_reason = WEATHER_REASON_NONE

    return RealityContextResult(
        time_anchor=time_anchor,
        weather_anchor=weather_anchor,
        time_anchor_reason=reason if time_anchor else REASON_NONE,
        weather_anchor_reason=weather_reason,
        weather_status=weather_status,
    ).as_dict()


def prepend_reality_to_provider_content(
    content: Any,
    reality: Mapping[str, Any] | RealityContextResult | None,
) -> Any:
    """Prefix Reality Context without parsing user-controlled text.

    Daily cold time anchors are inserted structurally by
    format_resident_turn_content; this helper remains the prefix path for
    Forge and non-cold payloads.
    """
    if isinstance(reality, RealityContextResult):
        prefix = reality.provider_prefix()
    elif isinstance(reality, Mapping):
        parts = [
            str(reality.get('time_anchor') or '').strip(),
            str(reality.get('weather_anchor') or '').strip(),
        ]
        prefix = '\n\n'.join(p for p in parts if p)
    else:
        prefix = ''
    if not prefix:
        return content

    if isinstance(content, str):
        body = content
        if not body:
            return prefix
        return prefix + '\n\n' + body

    if isinstance(content, list):
        # Multimodal Claude content: prepend into the first text block, or insert one.
        out = [dict(block) if isinstance(block, dict) else block for block in content]
        for block in out:
            if isinstance(block, dict) and block.get('type') == 'text':
                existing = str(block.get('text') or '')
                block['text'] = prefix + ('\n\n' + existing if existing else '')
                return out
        out.insert(0, {'type': 'text', 'text': prefix})
        return out

    return content
