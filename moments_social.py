"""Like/dislike/comment persistence for moments feed items."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from moments_store import parse_item_key, to_iso8601_shanghai

_DEFAULT_REACTOR = 'haya'
_SHANGHAI = timezone(timedelta(hours=8))
_MAX_COMMENT_LEN = 500


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now_str() -> str:
    return datetime.now(_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')


def ensure_schema(memories_db_path: str) -> None:
    conn = sqlite3.connect(memories_db_path)
    try:
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS moment_reactions (
                item_key TEXT NOT NULL,
                reactor TEXT NOT NULL DEFAULT 'haya',
                reaction TEXT NOT NULL CHECK(reaction IN ('like', 'dislike')),
                created_at TEXT NOT NULL,
                PRIMARY KEY (item_key, reactor)
            )'''
        )
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS moment_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_key TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'haya',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )'''
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_moment_comments_item_key '
            'ON moment_comments(item_key, id DESC)'
        )
        conn.commit()
    finally:
        conn.close()


def _validate_item_key(item_key: str) -> str:
    key = (item_key or '').strip()
    parse_item_key(key)
    return key


def _thought_exists_on_conn(conn: sqlite3.Connection, thought_id: int) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts'"
    ).fetchone()
    if not table:
        return False
    row = conn.execute(
        "SELECT 1 FROM posts WHERE id=? AND type='THOUGHT'",
        (int(thought_id),),
    ).fetchone()
    return row is not None


def _chat_collection_exists_on_conn(conn: sqlite3.Connection, collection_id: int) -> bool:
    row = conn.execute(
        'SELECT 1 FROM moment_chat_collections WHERE id=?',
        (int(collection_id),),
    ).fetchone()
    return row is not None


def _gallery_exists_on_conn(
    conn: sqlite3.Connection,
    pid: str,
    *,
    gallery_db_path: str | None,
) -> bool:
    if not gallery_db_path:
        return False
    conn.execute('ATTACH DATABASE ? AS gallery_db', (gallery_db_path,))
    row = conn.execute(
        'SELECT 1 FROM gallery_db.gallery_photos WHERE pid=?',
        (pid,),
    ).fetchone()
    return row is not None


def _detach_gallery_db(conn: sqlite3.Connection) -> None:
    try:
        conn.execute('DETACH DATABASE gallery_db')
    except sqlite3.OperationalError:
        pass


def _assert_item_exists_on_conn(
    conn: sqlite3.Connection,
    item_key: str,
    *,
    gallery_db_path: str | None = None,
) -> str:
    key = _validate_item_key(item_key)
    kind, ref = parse_item_key(key)
    if kind == 'thought':
        if not _thought_exists_on_conn(conn, int(ref)):
            raise LookupError('item not found')
        return key
    if kind == 'chat-collection':
        if not _chat_collection_exists_on_conn(conn, int(ref)):
            raise LookupError('item not found')
        return key
    if kind == 'gallery':
        if not _gallery_exists_on_conn(conn, ref, gallery_db_path=gallery_db_path):
            _detach_gallery_db(conn)
            raise LookupError('item not found')
        return key
    raise LookupError('item not found')


def item_exists(
    item_key: str,
    *,
    memories_db_path: str,
    gallery_db_path: str | None = None,
) -> bool:
    key = _validate_item_key(item_key)
    kind, ref = parse_item_key(key)
    if kind == 'thought':
        conn = _conn(memories_db_path)
        try:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts'"
            ).fetchone()
            if not table:
                return False
            row = conn.execute(
                "SELECT 1 FROM posts WHERE id=? AND type='THOUGHT'",
                (int(ref),),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    if kind == 'chat-collection':
        conn = _conn(memories_db_path)
        try:
            row = conn.execute(
                'SELECT 1 FROM moment_chat_collections WHERE id=?',
                (int(ref),),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    if kind == 'gallery':
        if not gallery_db_path:
            return False
        conn = _conn(gallery_db_path)
        try:
            row = conn.execute(
                'SELECT 1 FROM gallery_photos WHERE pid=?',
                (ref,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    return False


def require_item_exists(
    item_key: str,
    *,
    memories_db_path: str,
    gallery_db_path: str | None = None,
) -> str:
    key = _validate_item_key(item_key)
    if not item_exists(key, memories_db_path=memories_db_path, gallery_db_path=gallery_db_path):
        raise LookupError('item not found')
    return key


def delete_social_for_item(item_key: str, *, memories_db_path: str) -> None:
    key = _validate_item_key(item_key)
    conn = _conn(memories_db_path)
    try:
        conn.execute('DELETE FROM moment_reactions WHERE item_key=?', (key,))
        conn.execute('DELETE FROM moment_comments WHERE item_key=?', (key,))
        conn.commit()
    finally:
        conn.close()


def get_item_social(
    item_key: str,
    *,
    memories_db_path: str,
    reactor: str = _DEFAULT_REACTOR,
) -> dict[str, Any]:
    key = _validate_item_key(item_key)
    actor = (reactor or _DEFAULT_REACTOR).strip() or _DEFAULT_REACTOR
    conn = _conn(memories_db_path)
    try:
        likes = conn.execute(
            "SELECT COUNT(*) FROM moment_reactions WHERE item_key=? AND reaction='like'",
            (key,),
        ).fetchone()[0]
        dislikes = conn.execute(
            "SELECT COUNT(*) FROM moment_reactions WHERE item_key=? AND reaction='dislike'",
            (key,),
        ).fetchone()[0]
        comments = conn.execute(
            'SELECT COUNT(*) FROM moment_comments WHERE item_key=?',
            (key,),
        ).fetchone()[0]
        mine = conn.execute(
            'SELECT reaction FROM moment_reactions WHERE item_key=? AND reactor=?',
            (key, actor),
        ).fetchone()
    finally:
        conn.close()
    return {
        'likes': int(likes or 0),
        'dislikes': int(dislikes or 0),
        'comments': int(comments or 0),
        'my_reaction': mine['reaction'] if mine else None,
    }


def attach_social_to_items(
    items: list[dict[str, Any]],
    *,
    memories_db_path: str,
    reactor: str = _DEFAULT_REACTOR,
) -> list[dict[str, Any]]:
    if not items:
        return items
    keys = [item['item_key'] for item in items]
    actor = (reactor or _DEFAULT_REACTOR).strip() or _DEFAULT_REACTOR
    placeholders = ','.join('?' * len(keys))
    conn = _conn(memories_db_path)
    try:
        like_rows = conn.execute(
            f"SELECT item_key, COUNT(*) AS c FROM moment_reactions "
            f"WHERE item_key IN ({placeholders}) AND reaction='like' GROUP BY item_key",
            keys,
        ).fetchall()
        dislike_rows = conn.execute(
            f"SELECT item_key, COUNT(*) AS c FROM moment_reactions "
            f"WHERE item_key IN ({placeholders}) AND reaction='dislike' GROUP BY item_key",
            keys,
        ).fetchall()
        comment_rows = conn.execute(
            f'SELECT item_key, COUNT(*) AS c FROM moment_comments '
            f'WHERE item_key IN ({placeholders}) GROUP BY item_key',
            keys,
        ).fetchall()
        mine_rows = conn.execute(
            f'SELECT item_key, reaction FROM moment_reactions '
            f'WHERE item_key IN ({placeholders}) AND reactor=?',
            [*keys, actor],
        ).fetchall()
    finally:
        conn.close()

    likes_map = {row['item_key']: int(row['c']) for row in like_rows}
    dislikes_map = {row['item_key']: int(row['c']) for row in dislike_rows}
    comments_map = {row['item_key']: int(row['c']) for row in comment_rows}
    mine_map = {row['item_key']: row['reaction'] for row in mine_rows}

    enriched: list[dict[str, Any]] = []
    for item in items:
        key = item['item_key']
        social = {
            'likes': likes_map.get(key, 0),
            'dislikes': dislikes_map.get(key, 0),
            'comments': comments_map.get(key, 0),
            'my_reaction': mine_map.get(key),
        }
        copy = dict(item)
        copy['social'] = social
        enriched.append(copy)
    return enriched


def toggle_reaction(
    item_key: str,
    reaction: str,
    *,
    memories_db_path: str,
    gallery_db_path: str | None = None,
    reactor: str = _DEFAULT_REACTOR,
) -> dict[str, Any]:
    key = _validate_item_key(item_key)
    kind = (reaction or '').strip().lower()
    if kind not in {'like', 'dislike'}:
        raise ValueError('reaction must be like or dislike')
    actor = (reactor or _DEFAULT_REACTOR).strip() or _DEFAULT_REACTOR

    conn = _conn(memories_db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _assert_item_exists_on_conn(conn, key, gallery_db_path=gallery_db_path)
        current = conn.execute(
            'SELECT reaction FROM moment_reactions WHERE item_key=? AND reactor=?',
            (key, actor),
        ).fetchone()
        if current and current['reaction'] == kind:
            conn.execute(
                'DELETE FROM moment_reactions WHERE item_key=? AND reactor=?',
                (key, actor),
            )
        else:
            conn.execute(
                '''INSERT INTO moment_reactions (item_key, reactor, reaction, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(item_key, reactor) DO UPDATE SET
                     reaction=excluded.reaction,
                     created_at=excluded.created_at''',
                (key, actor, kind, _now_str()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _detach_gallery_db(conn)
        conn.close()
    return get_item_social(key, memories_db_path=memories_db_path, reactor=actor)


def list_comments(
    item_key: str,
    *,
    memories_db_path: str,
    gallery_db_path: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    key = require_item_exists(
        item_key,
        memories_db_path=memories_db_path,
        gallery_db_path=gallery_db_path,
    )
    if limit < 1 or limit > 100:
        raise ValueError('limit must be between 1 and 100')
    conn = _conn(memories_db_path)
    try:
        rows = conn.execute(
            'SELECT id, author, content, created_at FROM moment_comments '
            'WHERE item_key=? ORDER BY id DESC LIMIT ?',
            (key, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            'id': int(row['id']),
            'author': row['author'],
            'content': row['content'],
            'created_at': to_iso8601_shanghai(row['created_at']),
        }
        for row in rows
    ]


def add_comment(
    item_key: str,
    content: str,
    *,
    memories_db_path: str,
    gallery_db_path: str | None = None,
    author: str = _DEFAULT_REACTOR,
) -> dict[str, Any]:
    key = _validate_item_key(item_key)
    text = (content or '').strip()
    if not text:
        raise ValueError('content required')
    if len(text) > _MAX_COMMENT_LEN:
        raise ValueError(f'content exceeds {_MAX_COMMENT_LEN} characters')
    actor = (author or _DEFAULT_REACTOR).strip() or _DEFAULT_REACTOR

    conn = _conn(memories_db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _assert_item_exists_on_conn(conn, key, gallery_db_path=gallery_db_path)
        cur = conn.execute(
            'INSERT INTO moment_comments (item_key, author, content, created_at) VALUES (?, ?, ?, ?)',
            (key, actor, text, _now_str()),
        )
        comment_id = int(cur.lastrowid)
        row = conn.execute(
            'SELECT id, author, content, created_at FROM moment_comments WHERE id=?',
            (comment_id,),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _detach_gallery_db(conn)
        conn.close()
    if not row:
        raise RuntimeError('comment insert failed')
    social = get_item_social(key, memories_db_path=memories_db_path, reactor=actor)
    return {
        'comment': {
            'id': int(row['id']),
            'author': row['author'],
            'content': row['content'],
            'created_at': to_iso8601_shanghai(row['created_at']),
        },
        'social': social,
    }
