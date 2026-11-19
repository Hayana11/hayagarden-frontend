#!/usr/bin/env python3
"""Dry-run-first backfill of completed API Relay turns into the current window."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat.context_window import resolve_canonical_context_row_conn
from chat.window_identity import identity_from_context_row, normalize_window_identity
from continuity.sources import derive_completed_turns

CHAT_COLUMNS = (
    'id', 'author', 'content', 'created_at', 'tool_calls', 'branches',
    'branch_idx', 'cache_info', 'source_kind', 'attachments', 'image_url',
    'file_url', 'file_name',
)


class MappingConflict(RuntimeError):
    pass


def _open_read_only(db_path: str) -> sqlite3.Connection:
    path = str(Path(db_path).expanduser().resolve())
    conn = sqlite3.connect('file:%s?mode=ro' % quote(path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _message_rows(conn: sqlite3.Connection, boundary: int) -> list[dict]:
    columns = {str(row['name']) for row in conn.execute('PRAGMA table_info(chat_messages)')}
    required = {'id', 'author', 'content', 'created_at'}
    if required - columns:
        raise RuntimeError('chat_messages schema is missing required columns')
    selected = [name for name in CHAT_COLUMNS if name in columns]
    return [dict(row) for row in conn.execute(
        'SELECT %s FROM chat_messages WHERE id>? ORDER BY id ASC' % ', '.join(selected),
        (int(boundary),),
    )]


def _provider(row: dict) -> str:
    try:
        cache = json.loads(row.get('cache_info') or '{}')
    except (TypeError, ValueError):
        return ''
    return str(cache.get('provider') or '').strip().lower() if isinstance(cache, dict) else ''


def _message_id(source_ref: str) -> int:
    return int(str(source_ref).rsplit(':', 1)[1])


def _build_plan(db_path: str) -> dict:
    conn = _open_read_only(db_path)
    try:
        context = resolve_canonical_context_row_conn(conn, chat_id='default')
        identity = normalize_window_identity(
            identity_from_context_row(dict(context), chat_id='default')
        )
        if identity is None:
            raise RuntimeError('canonical current window identity unavailable')
        boundary = int(context.get('boundary_message_id') or 0)
        rows = _message_rows(conn, boundary)
        by_id = {int(row['id']): row for row in rows}
        turns = derive_completed_turns(rows, chat_id='default')
        api_turns = []
        for turn in turns:
            user_id = _message_id(turn.user_input_ref.source_ref)
            assistant_id = _message_id(turn.assistant_committed_output_refs[0].source_ref)
            if assistant_id in by_id and _provider(by_id[assistant_id]) == 'api_relay':
                api_turns.append((user_id, assistant_id))

        ids = {mid for pair in api_turns for mid in pair}
        existing = {}
        if ids:
            marks = ','.join('?' for _ in ids)
            existing = {
                int(row['message_id']): dict(row)
                for row in conn.execute(
                    'SELECT message_id,context_id,context_epoch,resident_generation,role '
                    'FROM daily_message_contexts WHERE message_id IN (%s)' % marks,
                    tuple(sorted(ids)),
                )
            }

        pending = []
        fully_unmapped = partially_mapped = 0
        for user_id, assistant_id in api_turns:
            mapped = []
            for message_id, role in ((user_id, 'user'), (assistant_id, 'assistant')):
                row = existing.get(message_id)
                if row is None:
                    pending.append({'message_id': message_id, 'role': role})
                    mapped.append(False)
                elif (
                    int(row['context_id']) != identity['context_id']
                    or int(row['context_epoch']) != identity['context_epoch']
                    or str(row['role']) != role
                ):
                    raise MappingConflict(
                        'message %s is already bound to context/epoch %s/%s with role %s'
                        % (message_id, row['context_id'], row['context_epoch'], row['role'])
                    )
                else:
                    mapped.append(True)
            if mapped == [False, False]:
                fully_unmapped += 1
            elif mapped == [True, False] or mapped == [False, True]:
                partially_mapped += 1

        return {
            'summary': {
                'chat_id': identity['chat_id'],
                'boundary_message_id': boundary,
                'target_identity': identity,
                'api_relay_completed_turns': len(api_turns),
                'fully_unmapped_turns': fully_unmapped,
                'partially_mapped_turns': partially_mapped,
                'pending_messages': len(pending),
                'pending_user_messages': sum(x['role'] == 'user' for x in pending),
                'pending_assistant_messages': sum(x['role'] == 'assistant' for x in pending),
            },
            'pending': pending,
        }
    finally:
        conn.close()


def run_backfill(db_path: str, *, apply: bool = False) -> dict:
    plan = _build_plan(db_path)
    applied = 0
    if apply:
        from chat.daily_context import record_daily_message_context

        identity = plan['summary']['target_identity']
        for item in plan['pending']:
            record_daily_message_context(
                item['message_id'],
                context_id=identity['context_id'],
                context_epoch=identity['context_epoch'],
                resident_generation=identity['resident_generation'],
                role=item['role'],
                db_path=db_path,
            )
            applied += 1
    return {**plan['summary'], 'applied_messages': applied}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='/opt/frontend/memories.db')
    parser.add_argument('--apply', action='store_true', help='write the planned mappings')
    args = parser.parse_args(argv)
    try:
        result = run_backfill(args.db, apply=args.apply)
    except MappingConflict as exc:
        print(json.dumps({'error': 'mapping_conflict', 'detail': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({'mode': 'apply' if args.apply else 'dry-run', **result},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
