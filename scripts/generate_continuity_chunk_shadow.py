"""Explicit, human-triggered R3 shadow chunk runner.

The source database is opened read-only and the shadow store path is always
explicit.  Importing this module has no side effects.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from continuity.chunk_generation import generate_continuity_chunk
from continuity.store import ensure_schema


_SOURCE_COLUMNS = (
    'id, author, content, thinking, created_at, tool_calls, branches, branch_idx, '
    'cache_info, source_kind, attachments, image_url, file_url, file_name'
)


def _read_only_connection(path: str) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    return sqlite3.connect(f'file:{resolved.as_posix()}?mode=ro', uri=True)


def _source_rows(conn: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    cursor = conn.execute(f'SELECT {_SOURCE_COLUMNS} FROM chat_messages ORDER BY id')
    rows = cursor.fetchall()
    columns = tuple(item[0] for item in cursor.description)
    return tuple(dict(zip(columns, row)) for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Generate one Continuity R3 shadow chunk')
    parser.add_argument('--source-db', required=True, help='read-only chat source database')
    parser.add_argument('--store-db', required=True, help='explicit isolated continuity shadow store')
    parser.add_argument('--generation-job-id', required=True)
    args = parser.parse_args(argv)

    source_conn = _read_only_connection(args.source_db)
    try:
        rows = _source_rows(source_conn)
    finally:
        source_conn.close()

    store_conn = sqlite3.connect(str(Path(args.store_db).expanduser()))
    store_conn.row_factory = sqlite3.Row
    try:
        ensure_schema(store_conn)
        # The canonical provider authority and adapter are injected by the
        # orchestration defaults.  This runner is never imported by runtime
        # request, Wake, cron, or server startup code.
        chunk = generate_continuity_chunk(
            store_conn,
            args.generation_job_id,
            rows_provider=lambda: rows,
        )
        print(json.dumps({
            'status': chunk.status,
            'chunk_id': chunk.chunk_id,
            'generation_job_id': chunk.generation_job_id,
            'candidate_id': chunk.candidate_id,
            'source_token_estimate': chunk.source_token_estimate,
            'output_token_estimate': chunk.output_token_estimate,
            'provider': chunk.provider,
            'model_identity': chunk.model_identity,
            'actual_executor': chunk.actual_executor,
        }, ensure_ascii=False, sort_keys=True))
    finally:
        store_conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

