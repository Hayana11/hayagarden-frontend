"""Staged chat rewrite intents (regen / edit) for activation-atomic switches.

Active transcript stays untouched until a candidate assistant is durable in
staging and activate/finalize commits the authoritative switch in one txn.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Mapping, Optional


STATUS_PREPARED = 'prepared'
STATUS_GENERATING = 'generating'
STATUS_READY = 'ready'
STATUS_ACTIVATED = 'activated'
STATUS_FAILED = 'failed'

OP_REGEN = 'regen'
OP_EDIT = 'edit'

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_rewrite_staging (
    rewrite_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    user_message_id INTEGER,
    edited_content TEXT,
    old_branches_json TEXT,
    tail_archive_json TEXT,
    source_snapshot_json TEXT,
    candidate_content TEXT,
    candidate_thinking TEXT,
    candidate_tool_calls TEXT,
    candidate_cache_info TEXT,
    candidate_choices TEXT,
    error TEXT,
    created_at REAL,
    updated_at REAL
)
"""


def ensure_schema(conn) -> None:
    conn.execute(_SCHEMA_SQL)
    conn.commit()


def ensure_schema_for_path(db_path: str) -> None:
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()


def _now() -> float:
    return time.time()


def _row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return dict(row)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(raw: Any, default):
    if raw is None or raw == '':
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def load(conn, rewrite_id: str) -> Optional[dict]:
    rid = (rewrite_id or '').strip()
    if not rid:
        return None
    row = conn.execute(
        'SELECT * FROM chat_rewrite_staging WHERE rewrite_id=?', (rid,)
    ).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def _set_status(conn, rewrite_id: str, status: str, **fields) -> None:
    cols = ['status=?', 'updated_at=?']
    vals: list[Any] = [status, _now()]
    for key, value in fields.items():
        cols.append(f'{key}=?')
        vals.append(value)
    vals.append(rewrite_id)
    conn.execute(
        f"UPDATE chat_rewrite_staging SET {', '.join(cols)} WHERE rewrite_id=?",
        vals,
    )


def mark_generating(conn, rewrite_id: str) -> None:
    _set_status(conn, rewrite_id, STATUS_GENERATING)


def mark_failed(conn, rewrite_id: str, error: str) -> None:
    _set_status(conn, rewrite_id, STATUS_FAILED, error=str(error or '')[:500])


def store_candidate(
    conn,
    rewrite_id: str,
    *,
    content: str,
    thinking: str = '',
    tool_calls: str = '',
    cache_info: str = '',
    choices: str = '',
) -> None:
    text = (content or '').strip()
    if not text:
        raise ValueError('candidate content required')
    row = load(conn, rewrite_id)
    if not row:
        raise ValueError('rewrite not found')
    if row.get('status') == STATUS_ACTIVATED:
        raise ValueError('rewrite already activated')
    _set_status(
        conn,
        rewrite_id,
        STATUS_READY,
        candidate_content=text,
        candidate_thinking=thinking or '',
        candidate_tool_calls=tool_calls or '',
        candidate_cache_info=cache_info or '',
        candidate_choices=choices or '',
        error='',
    )


def prepare_regen(conn, *, source_assistant_id: int) -> dict:
    ensure_schema(conn)
    row = conn.execute(
        'SELECT * FROM chat_messages WHERE id=?', (int(source_assistant_id),)
    ).fetchone()
    if not row:
        raise KeyError('source assistant not found')
    row_d = _row_to_dict(row)
    author = row_d.get('author')
    if author not in ('fyodor', 'assistant', 'claude'):
        raise ValueError('source must be an assistant message')

    old_branches = _loads(row_d.get('branches'), [])
    if not old_branches:
        old_branches = [{
            'content': row_d.get('content') or '',
            'thinking': row_d.get('thinking') or '',
            'tool_calls': row_d.get('tool_calls') or '',
        }]

    from chat.scoring_identity import find_user_message_before
    user_message_id = find_user_message_before(conn, int(source_assistant_id))

    rewrite_id = secrets.token_hex(16)
    now = _now()
    conn.execute(
        '''INSERT INTO chat_rewrite_staging (
            rewrite_id, operation, status, source_message_id, user_message_id,
            edited_content, old_branches_json, tail_archive_json, source_snapshot_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            rewrite_id,
            OP_REGEN,
            STATUS_PREPARED,
            int(source_assistant_id),
            int(user_message_id) if user_message_id is not None else None,
            None,
            _dumps(old_branches),
            None,
            _dumps(row_d),
            now,
            now,
        ),
    )
    return {
        'rewrite_id': rewrite_id,
        'operation': OP_REGEN,
        'source_assistant_id': int(source_assistant_id),
        'user_message_id': int(user_message_id) if user_message_id is not None else None,
        'old_branches': old_branches,
    }


def prepare_edit(conn, *, source_message_id: int, edited_content: str) -> dict:
    ensure_schema(conn)
    text = (edited_content or '').strip()
    if not text:
        raise ValueError('edited content required')
    row = conn.execute(
        'SELECT * FROM chat_messages WHERE id=?', (int(source_message_id),)
    ).fetchone()
    if not row:
        raise KeyError('source message not found')
    row_d = _row_to_dict(row)
    author = row_d.get('author')
    if author in ('fyodor', 'assistant', 'claude'):
        raise ValueError('edit target must be a user message')

    tail_rows = conn.execute(
        'SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC',
        (int(source_message_id),),
    ).fetchall()
    tail_archive = [_row_to_dict(r) for r in tail_rows]

    rewrite_id = secrets.token_hex(16)
    now = _now()
    conn.execute(
        '''INSERT INTO chat_rewrite_staging (
            rewrite_id, operation, status, source_message_id, user_message_id,
            edited_content, old_branches_json, tail_archive_json, source_snapshot_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            rewrite_id,
            OP_EDIT,
            STATUS_PREPARED,
            int(source_message_id),
            int(source_message_id),
            text,
            None,
            _dumps(tail_archive),
            _dumps(row_d),
            now,
            now,
        ),
    )
    return {
        'rewrite_id': rewrite_id,
        'operation': OP_EDIT,
        'source_message_id': int(source_message_id),
        'edited_content': text,
    }


def apply_history_overlay(rows: list[Any], staging: Mapping[str, Any]) -> list[Any]:
    """Return history rows as the model should see them for this staged rewrite.

    regen: drop source assistant and everything after it.
    edit: drop source user and everything after it; append synthetic edited user.
    """
    op = staging.get('operation')
    source_id = int(staging.get('source_message_id') or 0)
    if source_id <= 0:
        return list(rows)

    def _id(r: Any) -> int:
        if isinstance(r, dict):
            return int(r.get('id') or 0)
        try:
            return int(r['id'])
        except Exception:
            return int(r[0])

    kept = [r for r in rows if _id(r) < source_id]
    if op == OP_REGEN:
        return kept
    if op == OP_EDIT:
        snap = _loads(staging.get('source_snapshot_json'), {})
        edited = {
            'id': source_id,
            'author': snap.get('author') or 'hayana',
            'content': staging.get('edited_content') or '',
            'image_url': snap.get('image_url') or '',
            'created_at': snap.get('created_at') or '',
            'tool_calls': '',
            'file_url': snap.get('file_url') or '',
            'file_name': snap.get('file_name') or '',
        }
        return kept + [edited]
    return list(rows)


def _table_cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def activate_regen(conn, rewrite_id: str) -> dict:
    ensure_schema(conn)
    row = load(conn, rewrite_id)
    if not row:
        raise KeyError('rewrite not found')
    if row.get('operation') != OP_REGEN:
        raise ValueError('not a regen rewrite')
    if row.get('status') == STATUS_ACTIVATED:
        raise ValueError('already activated')
    if row.get('status') != STATUS_READY or not (row.get('candidate_content') or '').strip():
        raise ValueError('candidate not ready')

    source_id = int(row['source_message_id'])
    source = conn.execute(
        'SELECT * FROM chat_messages WHERE id=?', (source_id,)
    ).fetchone()
    if not source:
        raise KeyError('source assistant missing; refusing activate')

    old_branches = _loads(row.get('old_branches_json'), [])
    if not old_branches:
        src = _row_to_dict(source)
        old_branches = [{
            'content': src.get('content') or '',
            'thinking': src.get('thinking') or '',
            'tool_calls': src.get('tool_calls') or '',
        }]
    new_branch = {
        'content': row.get('candidate_content') or '',
        'thinking': row.get('candidate_thinking') or '',
        'tool_calls': row.get('candidate_tool_calls') or '',
    }
    all_branches = list(old_branches) + [new_branch]
    branch_idx = len(all_branches) - 1
    cols = _table_cols(conn, 'chat_messages')
    updates = {
        'content': new_branch['content'],
        'thinking': new_branch['thinking'],
        'tool_calls': new_branch['tool_calls'],
        'branches': _dumps(all_branches),
        'branch_idx': branch_idx,
    }
    if 'cache_info' in cols:
        updates['cache_info'] = row.get('candidate_cache_info') or ''
    if 'choices' in cols:
        updates['choices'] = row.get('candidate_choices') or ''
    assignments = ', '.join(f'{k}=?' for k in updates)
    conn.execute(
        f'UPDATE chat_messages SET {assignments} WHERE id=?',
        (*updates.values(), source_id),
    )
    _set_status(conn, rewrite_id, STATUS_ACTIVATED)
    return {
        'ok': True,
        'assistant_message_id': source_id,
        'branch_idx': branch_idx,
        'total': len(all_branches),
    }


def activate_edit(conn, rewrite_id: str) -> dict:
    ensure_schema(conn)
    row = load(conn, rewrite_id)
    if not row:
        raise KeyError('rewrite not found')
    if row.get('operation') != OP_EDIT:
        raise ValueError('not an edit rewrite')
    if row.get('status') == STATUS_ACTIVATED:
        raise ValueError('already activated')
    if row.get('status') != STATUS_READY or not (row.get('candidate_content') or '').strip():
        raise ValueError('candidate not ready')

    source_id = int(row['source_message_id'])
    source = conn.execute(
        'SELECT * FROM chat_messages WHERE id=?', (source_id,)
    ).fetchone()
    if not source:
        raise KeyError('source message missing; refusing activate')

    # Re-read live tail at activate time for archive fidelity; fall back to prepare snapshot.
    live_tail = conn.execute(
        'SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC',
        (source_id,),
    ).fetchall()
    if live_tail:
        tail_archive = [_row_to_dict(r) for r in live_tail]
        original_content = _row_to_dict(source).get('content') or ''
    else:
        tail_archive = _loads(row.get('tail_archive_json'), [])
        snap = _loads(row.get('source_snapshot_json'), {})
        original_content = snap.get('content') or ''

    conn.execute(
        'INSERT INTO chat_edit_branches (fork_msg_id, original_content, messages_json) '
        'VALUES (?, ?, ?)',
        (source_id, original_content, _dumps(tail_archive)),
    )

    snap = _loads(row.get('source_snapshot_json'), {})
    author = snap.get('author') or _row_to_dict(source).get('author') or 'hayana'
    image_url = snap.get('image_url') or ''
    file_url = snap.get('file_url') or ''
    file_name = snap.get('file_name') or ''
    edited = (row.get('edited_content') or '').strip()

    conn.execute('DELETE FROM chat_messages WHERE id >= ?', (source_id,))
    cols = _table_cols(conn, 'chat_messages')
    user_cols = ['author', 'content']
    user_vals: list[Any] = [author, edited]
    for col, val in (
        ('image_url', image_url),
        ('file_url', file_url),
        ('file_name', file_name),
    ):
        if col in cols:
            user_cols.append(col)
            user_vals.append(val)
    cur_u = conn.execute(
        f"INSERT INTO chat_messages ({', '.join(user_cols)}) VALUES ({', '.join('?' for _ in user_cols)})",
        user_vals,
    )
    new_user_id = int(cur_u.lastrowid)
    asst_cols = ['author', 'content']
    asst_vals: list[Any] = ['assistant', row.get('candidate_content') or '']
    for col, val in (
        ('thinking', row.get('candidate_thinking') or ''),
        ('tool_calls', row.get('candidate_tool_calls') or ''),
        ('cache_info', row.get('candidate_cache_info') or ''),
        ('choices', row.get('candidate_choices') or ''),
    ):
        if col in cols:
            asst_cols.append(col)
            asst_vals.append(val)
    cur_a = conn.execute(
        f"INSERT INTO chat_messages ({', '.join(asst_cols)}) VALUES ({', '.join('?' for _ in asst_cols)})",
        asst_vals,
    )
    new_assistant_id = int(cur_a.lastrowid)
    _set_status(conn, rewrite_id, STATUS_ACTIVATED)
    return {
        'ok': True,
        'message_id': new_user_id,
        'assistant_message_id': new_assistant_id,
    }


def active_transcript(conn) -> list[tuple]:
    """Helper for tests: (id, author, content) ordered."""
    rows = conn.execute(
        'SELECT id, author, content FROM chat_messages ORDER BY id ASC'
    ).fetchall()
    out = []
    for r in rows:
        if hasattr(r, 'keys'):
            out.append((int(r['id']), r['author'], r['content']))
        else:
            out.append((int(r[0]), r[1], r[2]))
    return out
