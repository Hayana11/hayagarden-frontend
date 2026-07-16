"""Moments feed store — multi-source projection over memories/gallery DBs."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
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
_HAYA_AUTHORS = {'hayana', 'user', 'haya'}
_FYODOR_AUTHORS = {'fyodor', 'assistant', 'claude'}
_LOG = logging.getLogger(__name__)
_SNAPSHOT_VERSION = 1

def _time_key_sql(column_expr: str) -> str:
    return (
        'CASE '
        f'WHEN datetime({column_expr}) IS NULL THEN NULL '
        f"WHEN {column_expr} GLOB '*Z' "
        f"OR {column_expr} GLOB '*[+-][0-9][0-9]:[0-9][0-9]' "
        f"THEN datetime({column_expr}, '+8 hours') "
        f'ELSE datetime({column_expr}) END'
    )


_CREATED_AT_KEY_SQL = _time_key_sql('created_at')
_INVALID_CREATED_AT_SQL = f'({_CREATED_AT_KEY_SQL}) IS NULL'
_INVALID_SORT_SQL = f'CASE WHEN {_INVALID_CREATED_AT_SQL} THEN 1 ELSE 0 END'

_GALLERY_TIME_SQL = _time_key_sql('COALESCE(saved_at, created_at)')
_INVALID_GALLERY_TIME_SQL = f'({_GALLERY_TIME_SQL}) IS NULL'
_INVALID_GALLERY_SORT_SQL = f'CASE WHEN {_INVALID_GALLERY_TIME_SQL} THEN 1 ELSE 0 END'

_REPOST_TIME_SQL = 'datetime(collected_at)'
_INVALID_REPOST_TIME_SQL = f'({_REPOST_TIME_SQL}) IS NULL'
_INVALID_REPOST_SORT_SQL = f'CASE WHEN {_INVALID_REPOST_TIME_SQL} THEN 1 ELSE 0 END'


def ensure_schema(memories_db_path: str, gallery_db_path: str | None = None) -> None:
    del gallery_db_path
    conn = sqlite3.connect(memories_db_path)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS moment_chat_collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                source_start_id INTEGER,
                source_end_id INTEGER,
                source_fingerprint TEXT NOT NULL UNIQUE,
                caption TEXT NOT NULL DEFAULT '',
                snapshot_json TEXT NOT NULL,
                collector TEXT NOT NULL DEFAULT 'fyodor',
                collected_at TEXT NOT NULL
            )'''
        )
        from moments_turn import ensure_turn_schema
        from moments_social import ensure_schema as ensure_social_schema
        ensure_turn_schema(memories_db_path)
        ensure_social_schema(memories_db_path)
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


def _db_created_at(value: str | None) -> str | None:
    dt = parse_beijing_datetime(value)
    if dt is None:
        return None
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(',') if part.strip()]


def _parse_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _brewing_from_row(row: sqlite3.Row, has_processed: bool) -> bool:
    if not has_processed:
        return False
    return int(row['processed'] or 0) == 0


_KIND_RANK = {
    'gallery': 0,
    'chat-collection': 1,
    'thought': 2,
}


def _tiebreak_parts(item_key: str) -> tuple[int, int | str]:
    kind, ref = parse_item_key(item_key)
    rank = _KIND_RANK[kind]
    if kind in ('thought', 'chat-collection'):
        return rank, int(ref)
    return rank, ref


def _feed_sort_key(published_at: str | None, item_key: str) -> tuple[int, str, int, int | str]:
    rank, tie = _tiebreak_parts(item_key)
    tier = 1 if published_at else 0
    return (tier, published_at or '', rank, tie)


def _cross_source_cursor_clause(
    *,
    time_sql: str,
    source_rank: int,
    tiebreak_sql: str,
    tiebreak_type: str,
    invalid_sql: str,
    cursor_published_at: str | None,
    cursor_item_key: str | None,
) -> tuple[str, list[Any]]:
    if not cursor_item_key:
        return '', []
    cursor_rank, cursor_tie = _tiebreak_parts(cursor_item_key)
    if tiebreak_type == 'int':
        cursor_tie_value = int(cursor_tie) if cursor_rank == source_rank else (
            2**62 if cursor_rank < source_rank else -1
        )
        tie_cmp = f'CAST({tiebreak_sql} AS INTEGER) < ?'
    else:
        cursor_tie_value = cursor_tie
        tie_cmp = f'{tiebreak_sql} < ?'

    same_time = f'((? < ?) OR (? = ? AND {tie_cmp}))'
    same_time_params: list[Any] = [source_rank, cursor_rank, source_rank, cursor_rank, cursor_tie_value]

    cursor_db = _db_created_at(cursor_published_at)
    if cursor_db:
        clause = (
            f'(({time_sql}) < datetime(?) '
            f'OR (({time_sql}) = datetime(?) AND {same_time}) '
            f'OR {invalid_sql})'
        )
        return clause, [cursor_db, cursor_db, *same_time_params]

    clause = f'({invalid_sql} AND {same_time})'
    return clause, same_time_params


def _normalize_role(author: str | None) -> str | None:
    name = (author or '').strip().lower()
    if name in _HAYA_AUTHORS:
        return 'haya'
    if name in _FYODOR_AUTHORS:
        return 'fyodor'
    return None


def _sanitize_snapshot_text(text: str | None) -> str:
    value = (text or '').strip()
    if not value:
        return ''
    value = re.sub(r'/opt/[^\s]+', '[path]', value)
    return value


def _message_attachment(row: sqlite3.Row) -> dict[str, Any] | None:
    image_url = (row['image_url'] or '').strip() if 'image_url' in row.keys() else ''
    file_url = (row['file_url'] or '').strip() if 'file_url' in row.keys() else ''
    file_name = (row['file_name'] or '').strip() if 'file_name' in row.keys() else ''
    if image_url:
        return {'kind': 'image', 'url': image_url}
    if file_url:
        return {'kind': 'file', 'url': file_url, 'name': file_name or '附件'}
    return None


def _build_snapshot_messages(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for row in rows:
        role = _normalize_role(row['author'])
        if not role:
            continue
        text = _sanitize_snapshot_text(row['content'])
        attachment = _message_attachment(row)
        if not text and not attachment:
            continue
        item: dict[str, Any] = {
            'message_id': int(row['id']),
            'role': role,
            'text': text,
            'created_at': to_iso8601_shanghai(row['created_at']),
        }
        if attachment:
            item['attachment'] = attachment
        messages.append(item)
    return messages


def _fingerprint_for_ids(message_ids: list[int]) -> str:
    payload = ','.join(str(mid) for mid in sorted(message_ids))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _now_str() -> str:
    return datetime.now(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')


def _load_snapshot(snapshot_json: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(snapshot_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get('version') != _SNAPSHOT_VERSION:
        return None
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return None
    return payload


def _source_label_from_messages(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        created_at = message.get('created_at')
        if not created_at:
            continue
        parts = shanghai_parts_from_iso(created_at)
        if not parts:
            continue
        return f"{parts['month']}月{parts['day']}日 {parts['hour']:02d}:{parts['minute']:02d}"
    return ''


def shanghai_parts_from_iso(iso: str | None) -> dict[str, int] | None:
    dt = parse_beijing_datetime(iso)
    if dt is None:
        return None
    return {
        'year': dt.year,
        'month': dt.month,
        'day': dt.day,
        'hour': dt.hour,
        'minute': dt.minute,
    }


def finalize_pending_chat_collection(
    *,
    memories_db_path: str,
    user_message_id: int,
    assistant_message_id: int,
    previous_turns: int,
    caption: str = '',
) -> int | None:
    turns = int(previous_turns)
    if turns < 0 or turns > 2:
        raise ValueError('previous_turns must be between 0 and 2')

    conn = _conn(memories_db_path)
    try:
        user_row = conn.execute(
            'SELECT id, session_id, author, content, created_at, image_url, file_url, file_name '
            'FROM chat_messages WHERE id=?',
            (int(user_message_id),),
        ).fetchone()
        assistant_row = conn.execute(
            'SELECT id, session_id, author, content, created_at, image_url, file_url, file_name '
            'FROM chat_messages WHERE id=?',
            (int(assistant_message_id),),
        ).fetchone()
        if not user_row or not assistant_row:
            return None
        if int(user_row['session_id'] or 1) != int(assistant_row['session_id'] or 1):
            return None

        session_id = int(user_row['session_id'] or 1)
        selected_ids = [int(user_row['id']), int(assistant_row['id'])]
        anchor = int(user_row['id'])
        for _ in range(turns):
            prior_assistant = conn.execute(
                'SELECT id FROM chat_messages WHERE session_id=? AND id < ? '
                "AND author IN ('fyodor','assistant','claude') ORDER BY id DESC LIMIT 1",
                (session_id, anchor),
            ).fetchone()
            if not prior_assistant:
                break
            prior_user = conn.execute(
                'SELECT id FROM chat_messages WHERE session_id=? AND id < ? '
                "AND author NOT IN ('fyodor','assistant','claude') ORDER BY id DESC LIMIT 1",
                (session_id, int(prior_assistant['id'])),
            ).fetchone()
            if not prior_user:
                break
            selected_ids = [int(prior_user['id']), int(prior_assistant['id'])] + selected_ids
            anchor = int(prior_user['id'])

        selected_ids = sorted(set(selected_ids))[-6:]
        placeholders = ','.join('?' for _ in selected_ids)
        rows = conn.execute(
            f'SELECT id, session_id, author, content, created_at, image_url, file_url, file_name '
            f'FROM chat_messages WHERE id IN ({placeholders}) ORDER BY id ASC',
            selected_ids,
        ).fetchall()
        if int(user_row['id']) not in selected_ids or int(assistant_row['id']) not in selected_ids:
            return None

        messages = _build_snapshot_messages(rows)
        if len(messages) < 2:
            return None
        fingerprint = _fingerprint_for_ids([int(row['id']) for row in rows])
        existing = conn.execute(
            'SELECT id FROM moment_chat_collections WHERE source_fingerprint=?',
            (fingerprint,),
        ).fetchone()
        if existing:
            return int(existing['id'])

        snapshot = {'version': _SNAPSHOT_VERSION, 'messages': messages}
        cur = conn.execute(
            '''INSERT INTO moment_chat_collections
               (session_id, source_start_id, source_end_id, source_fingerprint,
                caption, snapshot_json, collector, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, 'fyodor', ?)''',
            (
                session_id,
                selected_ids[0],
                int(assistant_message_id),
                fingerprint,
                (caption or '').strip()[:500],
                json.dumps(snapshot, ensure_ascii=False),
                _now_str(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _base_item(
    *,
    item_key: str,
    kind: str,
    author: str,
    content: str,
    created_at: str | None,
    post_id: int | None = None,
    collection_id: int | None = None,
    gallery_pid: str | None = None,
    tags: list[str] | None = None,
    brewing: bool = False,
    media: list[dict[str, Any]] | None = None,
    repost: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        'item_key': item_key,
        'post_id': post_id,
        'collection_id': collection_id,
        'gallery_pid': gallery_pid,
        'kind': kind,
        'author': author,
        'content': content,
        'title': None,
        'tags': tags or [],
        'brewing': brewing,
        'media': media or [],
        'created_at': created_at,
        'emotion': None,
        'repost': repost,
        'social': dict(_DEFAULT_SOCIAL),
    }


def _thought_item(row: sqlite3.Row, has_processed: bool) -> dict[str, Any]:
    created_iso = to_iso8601_shanghai(row['created_at'])
    return _base_item(
        item_key=f"thought:{row['id']}",
        kind='thought',
        author=(row['author'] or 'fyodor').strip() or 'fyodor',
        content=row['content'] or '',
        created_at=created_iso,
        post_id=int(row['id']),
        tags=_parse_tags(row['tags'] if 'tags' in row.keys() else None),
        brewing=_brewing_from_row(row, has_processed),
    )


def _repost_item(row: sqlite3.Row) -> dict[str, Any]:
    created_iso = to_iso8601_shanghai(row['collected_at'])
    snapshot = _load_snapshot(row['snapshot_json'])
    messages = snapshot.get('messages', []) if snapshot else []
    repost = {
        'source_label': _source_label_from_messages(messages),
        'messages': messages,
    }
    return _base_item(
        item_key=f"chat-collection:{row['id']}",
        kind='repost',
        author='fyodor',
        content=row['caption'] or '',
        created_at=created_iso,
        collection_id=int(row['id']),
        repost=repost,
    )


def _gallery_item(row: sqlite3.Row) -> dict[str, Any] | None:
    published_raw = row['saved_at'] or row['created_at']
    created_iso = to_iso8601_shanghai(published_raw)
    if not created_iso and not published_raw:
        return None
    note = (row['note'] or '').strip()
    summary = (row['summary'] or '').strip()
    content = note or summary
    keywords = _parse_keywords(row['keywords'] if 'keywords' in row.keys() else None)
    pid = row['pid']
    width = row['width'] if 'width' in row.keys() else None
    height = row['height'] if 'height' in row.keys() else None
    media = [{
        'pid': pid,
        'url': f'/api/gallery/photo/{pid}',
        'width': width,
        'height': height,
        'note': note or summary,
    }]
    return _base_item(
        item_key=f'gallery:{pid}',
        kind='gallery',
        author='fyodor',
        content=content,
        created_at=created_iso,
        gallery_pid=pid,
        tags=keywords,
        media=media,
    )


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
    cursor_clause, cursor_params = _cross_source_cursor_clause(
        time_sql=_CREATED_AT_KEY_SQL,
        source_rank=_KIND_RANK['thought'],
        tiebreak_sql='id',
        tiebreak_type='int',
        invalid_sql=_INVALID_CREATED_AT_SQL,
        cursor_published_at=cursor_published_at,
        cursor_item_key=cursor_item_key,
    )
    if cursor_clause:
        where.append(cursor_clause)
        params.extend(cursor_params)

    sql = (
        f"SELECT {', '.join(cols)} FROM posts WHERE {' AND '.join(where)} "
        f'ORDER BY {_INVALID_SORT_SQL}, ({_CREATED_AT_KEY_SQL}) DESC, id DESC LIMIT ?'
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_thought_item(row, has_processed) for row in rows]


def _fetch_repost_rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
    cursor_published_at: str | None,
    cursor_item_key: str | None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where: list[str] = []
    cursor_clause, cursor_params = _cross_source_cursor_clause(
        time_sql=_REPOST_TIME_SQL,
        source_rank=_KIND_RANK['chat-collection'],
        tiebreak_sql='id',
        tiebreak_type='int',
        invalid_sql=_INVALID_REPOST_TIME_SQL,
        cursor_published_at=cursor_published_at,
        cursor_item_key=cursor_item_key,
    )
    if cursor_clause:
        where.append(cursor_clause)
        params.extend(cursor_params)

    sql = (
        'SELECT id, caption, collected_at, snapshot_json FROM moment_chat_collections '
        f"{'WHERE ' + ' AND '.join(where) if where else ''} "
        f'ORDER BY {_INVALID_REPOST_SORT_SQL}, {_REPOST_TIME_SQL} DESC, id DESC LIMIT ?'
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_repost_item(row) for row in rows]


def _fetch_gallery_rows(
    gallery_db_path: str | None,
    *,
    limit: int,
    cursor_published_at: str | None,
    cursor_item_key: str | None,
) -> list[dict[str, Any]]:
    if not gallery_db_path:
        return []
    conn = _conn(gallery_db_path)
    try:
        params: list[Any] = []
        where: list[str] = []
        cursor_clause, cursor_params = _cross_source_cursor_clause(
            time_sql=_GALLERY_TIME_SQL,
            source_rank=_KIND_RANK['gallery'],
            tiebreak_sql='pid',
            tiebreak_type='text',
            invalid_sql=_INVALID_GALLERY_TIME_SQL,
            cursor_published_at=cursor_published_at,
            cursor_item_key=cursor_item_key,
        )
        if cursor_clause:
            where.append(cursor_clause)
            params.extend(cursor_params)

        sql = (
            'SELECT pid, note, summary, keywords, width, height, saved_at, created_at '
            'FROM gallery_photos '
            f"{'WHERE ' + ' AND '.join(where) if where else ''} "
            f'ORDER BY {_INVALID_GALLERY_SORT_SQL}, ({_GALLERY_TIME_SQL}) DESC, pid DESC LIMIT ?'
        )
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _gallery_item(row)
        if item:
            items.append(item)
    return items


def _merge_feed_items(
    *,
    memories_db_path: str,
    gallery_db_path: str | None,
    limit: int,
    feed_type: str,
    cursor_published_at: str | None,
    cursor_item_key: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    fetch_limit = limit + 1
    source_batches: list[list[dict[str, Any]]] = []

    conn = _conn(memories_db_path)
    try:
        post_cols = _posts_columns(conn)
        has_processed = 'processed' in post_cols
        has_tags = 'tags' in post_cols
        if feed_type in {'all', 'thought', 'posts'}:
            source_batches.append(_fetch_thought_rows(
                conn,
                limit=fetch_limit,
                cursor_published_at=cursor_published_at,
                cursor_item_key=cursor_item_key,
                has_processed=has_processed,
                has_tags=has_tags,
            ))
        if feed_type in {'all', 'posts'}:
            source_batches.append(_fetch_repost_rows(
                conn,
                limit=fetch_limit,
                cursor_published_at=cursor_published_at,
                cursor_item_key=cursor_item_key,
            ))
    finally:
        conn.close()

    if feed_type in {'all', 'gallery'}:
        source_batches.append(_fetch_gallery_rows(
            gallery_db_path,
            limit=fetch_limit,
            cursor_published_at=cursor_published_at,
            cursor_item_key=cursor_item_key,
        ))

    pool: list[dict[str, Any]] = []
    for batch in source_batches:
        pool.extend(batch)

    pool.sort(
        key=lambda item: _feed_sort_key(item.get('created_at'), item['item_key']),
        reverse=True,
    )
    has_more = len(pool) > limit or any(len(batch) >= fetch_limit for batch in source_batches)
    return pool[:limit], has_more


def get_feed(
    *,
    memories_db_path: str,
    gallery_db_path: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
    feed_type: str = 'all',
) -> dict[str, Any]:
    if limit < 1 or limit > 50:
        raise ValueError('limit must be between 1 and 50')
    normalized_type = (feed_type or 'all').strip().lower()
    if normalized_type not in {'all', 'thought', 'posts', 'gallery'}:
        raise ValueError('unsupported feed type')

    cursor_published_at: str | None = None
    cursor_item_key: str | None = None
    if cursor:
        cursor_published_at, cursor_item_key = decode_cursor(cursor)

    page, has_more = _merge_feed_items(
        memories_db_path=memories_db_path,
        gallery_db_path=gallery_db_path,
        limit=limit,
        feed_type=normalized_type,
        cursor_published_at=cursor_published_at,
        cursor_item_key=cursor_item_key,
    )

    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(last.get('created_at'), last['item_key'])

    from moments_social import attach_social_to_items
    page = attach_social_to_items(page, memories_db_path=memories_db_path)

    return {
        'items': page,
        'next_cursor': next_cursor,
        'has_more': has_more,
    }


def get_chat_collection(collection_id: int, *, memories_db_path: str) -> dict[str, Any] | None:
    conn = _conn(memories_db_path)
    try:
        row = conn.execute(
            'SELECT id, caption, collected_at, snapshot_json FROM moment_chat_collections WHERE id=?',
            (int(collection_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return _repost_item(row)


def delete_chat_collection(collection_id: int, *, memories_db_path: str) -> bool:
    conn = _conn(memories_db_path)
    try:
        cur = conn.execute(
            'DELETE FROM moment_chat_collections WHERE id=?',
            (int(collection_id),),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
