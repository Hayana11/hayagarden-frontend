"""Documents View — union of user uploads + assistant Artifacts.

Two storage truths stay separate:
  user_upload        → chat_messages.file_url + /static/uploads/files
  assistant_artifact → artifacts table + /opt/frontend/artifacts

This module only builds a stable library identity and delete contract.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import artifact_store
from chat.attachment_contract import (
    CHAT_FILE_URL_PREFIX,
    resolve_uploaded_file_url,
)

DB_PATH = '/opt/frontend/memories.db'
FILES_DIR = '/opt/frontend/static/uploads/files'
LIBRARY_LIMIT = 200
MAX_DELETE_KEYS = 50

_KEY_RE = re.compile(r'^(upload|artifact):(\d+)$')


def library_key(source: str, item_id: int) -> str:
    if source == 'user_upload':
        return 'upload:%d' % int(item_id)
    if source == 'assistant_artifact':
        return 'artifact:%d' % int(item_id)
    raise ValueError('unknown source: %s' % source)


def parse_library_key(key: Any) -> Optional[tuple[str, int]]:
    """Return (source, id) or None for illegal keys."""
    m = _KEY_RE.fullmatch(str(key or '').strip())
    if not m:
        return None
    kind, raw = m.group(1), m.group(2)
    try:
        n = int(raw)
    except ValueError:
        return None
    if n <= 0:
        return None
    source = 'user_upload' if kind == 'upload' else 'assistant_artifact'
    return source, n


def _upload_preview_url(file_url: str) -> str:
    name = os.path.basename(str(file_url or ''))
    if not name:
        return ''
    return '/api/chat/files/%s/preview' % quote(name, safe='')


def _upload_download_url(file_url: str) -> str:
    return str(file_url or '')


def _artifact_urls(meta: dict) -> tuple[str, str]:
    aid = int(meta['id'])
    download = '/api/artifacts/%d/download' % aid
    atype = str(meta.get('type') or '')
    if atype in ('html', 'markdown'):
        return '/api/artifacts/%d/preview' % aid, download
    return download, download


def _upload_size(file_url: str) -> Optional[int]:
    path = resolve_uploaded_file_url(file_url, FILES_DIR)
    if path is None or not path.is_file():
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


def _file_type_from_name(name: str, fallback: str = '') -> str:
    ext = Path(str(name or '')).suffix.lower().lstrip('.')
    return ext or fallback


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def list_documents(limit: int = LIBRARY_LIMIT) -> list[dict]:
    """Merge uploads + artifacts by created_at desc (stable library_key)."""
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = LIBRARY_LIMIT
    lim = max(1, min(lim, LIBRARY_LIMIT))

    conn = _db()
    try:
        uploads = conn.execute(
            "SELECT id, author, file_name, file_url, created_at, session_id "
            "FROM chat_messages WHERE file_url != '' "
            "ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
            (lim,),
        ).fetchall()
    finally:
        conn.close()

    artifacts = artifact_store.list_recent(limit=lim)

    items: list[dict] = []
    for row in uploads:
        mid = int(row['id'])
        file_url = str(row['file_url'] or '')
        file_name = str(row['file_name'] or '') or os.path.basename(file_url)
        items.append({
            'library_key': library_key('user_upload', mid),
            'source': 'user_upload',
            'id': mid,
            'author': row['author'],
            'file_name': file_name,
            'file_url': file_url,
            'created_at': row['created_at'],
            'session_id': row['session_id'],
            'file_type': _file_type_from_name(file_name),
            'size': _upload_size(file_url),
            'preview_url': _upload_preview_url(file_url),
            'download_url': _upload_download_url(file_url),
        })

    for meta in artifacts:
        aid = int(meta['id'])
        preview_url, download_url = _artifact_urls(meta)
        title = str(meta.get('title') or '') or ('artifact-%d' % aid)
        atype = str(meta.get('type') or '')
        ext = artifact_store.EXT_BY_TYPE.get(atype, atype)
        file_name = title if title.endswith('.' + ext) else ('%s.%s' % (title, ext))
        items.append({
            'library_key': library_key('assistant_artifact', aid),
            'source': 'assistant_artifact',
            'id': aid,
            'author': 'fyodor',
            'file_name': file_name,
            'created_at': meta.get('created_at'),
            'file_type': atype or ext,
            'size': meta.get('size'),
            'preview_url': preview_url,
            'download_url': download_url,
            'artifact_type': atype,
        })

    def _sort_key(item: dict):
        return (str(item.get('created_at') or ''), int(item.get('id') or 0))

    items.sort(key=_sort_key, reverse=True)
    return items[:lim]


def _delete_upload(conn: sqlite3.Connection, message_id: int) -> bool:
    row = conn.execute(
        'SELECT file_url FROM chat_messages WHERE id=?',
        (message_id,),
    ).fetchone()
    if not row or not row['file_url']:
        return False
    url = row['file_url']
    path = resolve_uploaded_file_url(url, FILES_DIR)
    if path is not None and path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
    conn.execute(
        "UPDATE chat_messages SET file_url='', file_name='' WHERE id=?",
        (message_id,),
    )
    return True


def delete_documents(
    *,
    keys: Optional[list] = None,
    ids: Optional[list] = None,
    strict_keys: bool = False,
) -> dict:
    """Delete by library keys and/or legacy upload message ids.

    ``ids`` are always user_upload message ids (never artifact ids).
    Illegal keys: skipped when ``strict_keys=False``; raise ValueError when True
    (route maps that to HTTP 400).

    Order is fixed to avoid SQLite write locks:
      1) parse / validate / dedupe
      2) delete Artifacts first (own connections)
      3) then one connection for all user-upload updates
    Input key order must not affect success.
    """
    parsed: list[tuple[str, int]] = []
    raw_keys = list(keys or [])
    if len(raw_keys) > MAX_DELETE_KEYS:
        raw_keys = raw_keys[:MAX_DELETE_KEYS]

    for key in raw_keys:
        got = parse_library_key(key)
        if got is None:
            if strict_keys:
                raise ValueError('invalid library key: %r' % (key,))
            continue
        parsed.append(got)

    for mid in list(ids or [])[:MAX_DELETE_KEYS]:
        try:
            n = int(mid)
        except (TypeError, ValueError):
            continue
        if n > 0:
            parsed.append(('user_upload', n))

    # Deduplicate while preserving first-seen order, then partition.
    seen = set()
    unique: list[tuple[str, int]] = []
    for item in parsed:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    unique = unique[:MAX_DELETE_KEYS]

    artifact_ids = [item_id for source, item_id in unique if source == 'assistant_artifact']
    upload_ids = [item_id for source, item_id in unique if source == 'user_upload']

    deleted_artifacts = 0
    for aid in artifact_ids:
        status = artifact_store.delete(aid, allow_stale_metadata=True)
        if status in ('deleted', 'stale_cleared'):
            deleted_artifacts += 1
        # unlink_failed / not_found / bad_path → do not count as deleted

    deleted_uploads = 0
    conn = _db()
    try:
        for mid in upload_ids:
            if _delete_upload(conn, mid):
                deleted_uploads += 1
        conn.commit()
    finally:
        conn.close()

    return {
        'ok': True,
        'deleted': deleted_uploads + deleted_artifacts,
        'deleted_uploads': deleted_uploads,
        'deleted_artifacts': deleted_artifacts,
    }
