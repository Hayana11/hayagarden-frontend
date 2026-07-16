"""Moments feed store — multi-source projection over memories/gallery DBs."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

SHANGHAI = timezone(timedelta(hours=8))
_ITEM_KEY_RE = re.compile(
    r'^(?P<kind>thought|chat-collection|gallery):(?P<ref>.+)$'
)
_DEFAULT_SOCIAL = {
    'likes': 0,
    'dislikes': 0,
    'comments': 0,
    'my_reaction': None,
}


def ensure_schema(memories_db_path: str, gallery_db_path: str | None = None) -> None:
    """Idempotent schema hook for future moments tables (PR1 is read-only)."""
    del gallery_db_path
    conn = sqlite3.connect(memories_db_path)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.commit()
    finally:
        conn.close()


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _posts_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute('PRAGMA table_info(posts)')}


def parse_beijing_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if 'T' in text:
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=SHANGHAI)
            return parsed.astimezone(SHANGHAI)
        except ValueError:
            return None
    for fmt, size in (('%Y-%m-%d %H:%M:%S', 19), ('%Y-%m-%d %H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            naive = datetime.strptime(text[:size], fmt)
            return naive.replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    return None


def to_iso8601_shanghai(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value.astimezone(SHANGHAI) if value.tzinfo else value.replace(tzinfo=SHANGHAI)
    else:
        dt = parse_beijing_datetime(value)
    if dt is None:
        return None
    return dt.astimezone(SHANGHAI).strftime('%Y-%m-%dT%H:%M:%S+08:00')


def parse_item_key(item_key: str) -> tuple[str, str]:
    match = _ITEM_KEY_RE.match((item_key or '').strip())
    if not match:
        raise ValueError('invalid item_key')
    return match.group('kind'), match.group('ref')


def encode_cursor(published_at: str | None, item_key: str) -> str:
    payload = {'published_at': published_at, 'item_key': item_key}
    raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_cursor(cursor: str) -> tuple[str | None, str]:
    if not cursor:
        raise ValueError('cursor required')
    padded = cursor + '=' * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError('invalid cursor') from exc
    if not isinstance(payload, dict):
        raise ValueError('invalid cursor')
    item_key = payload.get('item_key')
    if not isinstance(item_key, str) or not item_key:
        raise ValueError('invalid cursor')
    published_at = payload.get('published_at')
    if published_at is not None and not isinstance(published_at, str):
        raise ValueError('invalid cursor')
    parse_item_key(item_key)
    return published_at, item_key


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(',') if part.strip()]


def _brewing_from_row(row: sqlite3.Row, has_processed: bool) -> bool:
    if not has_processed:
        return False
    return int(row['processed'] or 0) == 0


def _thought_item(row: sqlite3.Row, has_processed: bool) -> dict[str, Any]:
    created_iso = to_iso8601_shanghai(row['created_at'])
    item_key = f"thought:{row['id']}"
    return {
        'item_key': item_key,
        'post_id': int(row['id']),
        'collection_id': None,
        'gallery_pid': None,
        'kind': 'thought',
        'author': (row['author'] or 'fyodor').strip() or 'fyodor',
        'content': row['content'] or '',
        'title': None,
        'tags': _parse_tags(row['tags'] if 'tags' in row.keys() else None),
        'brewing': _brewing_from_row(row, has_processed),
        'media': [],
        'created_at': created_iso,
        'emotion': None,
        'repost': None,
        'social': dict(_DEFAULT_SOCIAL),
        '_published_at': created_iso,
    }


def _db_created_at(value: str | None) -> str | None:
    dt = parse_beijing_datetime(value)
    if dt is None:
        return None
    return dt.strftime('%Y-%m-%d %H:%M:%S')



def _fetch_thought_rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
    cursor_published_at: str | None,
    cursor_item_key: str | None,
    has_processed: bool,
    has_tags: bool,
) -> list[dict[str, Any]]:
    cols = ['id', 'content', 'author', 'created_at']
    if has_tags:
        cols.append('tags')
    if has_processed:
        cols.append('processed')

    params: list[Any] = []
    where = ["type='THOUGHT'"]
    if cursor_item_key:
        kind, ref = parse_item_key(cursor_item_key)
        if kind != 'thought':
            raise ValueError('invalid cursor')
        cursor_id = int(ref)
        cursor_db_time = _db_created_at(cursor_published_at)
        if cursor_db_time:
            where.append(
                '(datetime(created_at) < datetime(?) '
                'OR (created_at = ? AND id < ?) '
                'OR created_at IS NULL OR created_at = ?)'
            )
            params.extend([cursor_db_time, cursor_db_time, cursor_id, ''])
        else:
            # Cursor is on a null/empty-date row: only continue within that tier.
            where.append('((created_at IS NULL OR created_at = ?) AND id < ?)')
            params.extend(['', cursor_id])

    sql = (
        f"SELECT {', '.join(cols)} FROM posts WHERE {' AND '.join(where)} "
        'ORDER BY CASE WHEN created_at IS NULL OR created_at = ? THEN 1 ELSE 0 END, '
        'datetime(created_at) DESC, id DESC LIMIT ?'
    )
    params.extend(['', limit])
    rows = conn.execute(sql, params).fetchall()
    return [_thought_item(row, has_processed) for row in rows]


def get_feed(
    *,
    memories_db_path: str,
    gallery_db_path: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
    feed_type: str = 'all',
) -> dict[str, Any]:
    del gallery_db_path
    if limit < 1 or limit > 50:
        raise ValueError('limit must be between 1 and 50')
    normalized_type = (feed_type or 'all').strip().lower()
    if normalized_type not in {'all', 'thought', 'posts'}:
        raise ValueError('unsupported feed type')

    cursor_published_at: str | None = None
    cursor_item_key: str | None = None
    if cursor:
        cursor_published_at, cursor_item_key = decode_cursor(cursor)

    conn = _conn(memories_db_path)
    try:
        post_cols = _posts_columns(conn)
        has_processed = 'processed' in post_cols
        has_tags = 'tags' in post_cols
        fetch_limit = limit + 1
        thoughts = _fetch_thought_rows(
            conn,
            limit=fetch_limit,
            cursor_published_at=cursor_published_at,
            cursor_item_key=cursor_item_key,
            has_processed=has_processed,
            has_tags=has_tags,
        )
    finally:
        conn.close()

    has_more = len(thoughts) > limit
    page = thoughts[:limit]
    for item in page:
        item.pop('_published_at', None)

    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(last.get('created_at'), last['item_key'])

    return {
        'items': page,
        'next_cursor': next_cursor,
        'has_more': has_more,
    }
