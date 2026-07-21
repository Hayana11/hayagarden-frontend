#!/usr/bin/env python3
"""Internal State v3 Shadow 管理 CLI（Phase 1A-4b）

显式 schema / bootstrap / status。禁止在评分事务内偷偷建表。

用法：
  python3 tools/internal_state_shadow_admin.py prepare-schema
  python3 tools/internal_state_shadow_admin.py status
  python3 tools/internal_state_shadow_admin.py bootstrap

环境：
  MEMORIES_DB                 默认 /opt/frontend/memories.db
  INTERNAL_STATE_V3_SHADOW_ENABLED
  INTERNAL_STATE_V3_SCORE_PROOF_ENABLED
  INTERNAL_STATE_V3_USER_EVENTS_ENABLED
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import internal_state as isv3
import internal_state_shadow as shadow
import internal_state_store as store


def _cmd_prepare_schema(db_path: str) -> int:
    path = isv3.memories_db_path(db_path)
    conn = store.open_store(path)
    try:
        before = store.get_journal_mode(conn)
        shadow.ensure_shadow_schema(conn)
        after = store.get_journal_mode(conn)
        if before != after:
            print(f'ERROR: journal_mode changed {before!r} -> {after!r}', file=sys.stderr)
            return 2
        ready = {
            'db_path': path,
            'journal_mode': after,
            'internal_state_v3': _table_exists(conn, 'internal_state_v3'),
            'internal_state_events': _table_exists(conn, 'internal_state_events'),
            'score_applied': shadow.score_proof_schema_ready(conn),
            'proof_health': _table_exists(conn, shadow.PROOF_HEALTH_TABLE),
            'bootstrapped': False,
        }
        # prepare-schema 绝不 bootstrap
        st = store.read_state(conn)
        ready['bootstrapped'] = st is not None
        print(json.dumps(ready, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _cmd_status(db_path: str) -> int:
    environ = os.environ
    health = shadow.get_shadow_health(db_path=db_path, environ=environ)
    conn = store.open_store(isv3.memories_db_path(db_path))
    try:
        proof = shadow.read_proof_health(conn)
        wm = None
        if shadow.score_proof_schema_ready(conn):
            try:
                wm = shadow.resolve_scored_watermark(conn)
            except Exception as exc:
                wm = f'error:{exc}'
        payload = {
            'flags': {
                'SHADOW_ENABLED': shadow.is_shadow_enabled(environ=environ),
                'SCORE_PROOF_ENABLED': shadow.is_score_proof_enabled(environ=environ),
                'USER_EVENTS_ENABLED': shadow.is_user_events_enabled(environ=environ),
                'USER_EVENTS_FLAG_RAW': (
                    str(environ.get(shadow.USER_EVENTS_ENABLED_ENV, '0')).strip() == '1'
                ),
            },
            'health': health.as_dict(),
            'proof_health': proof.as_dict(),
            'proof_max_message_id': wm,
        }
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_bootstrap(db_path: str) -> int:
    if not shadow.is_shadow_enabled():
        print(
            'ERROR: INTERNAL_STATE_V3_SHADOW_ENABLED must be 1',
            file=sys.stderr,
        )
        return 2
    result = shadow.ensure_bootstrapped(db_path=db_path)
    print(json.dumps({
        'ok': result.ok,
        'status': result.status,
        'error': result.error,
        'elapsed_ms': result.elapsed_ms,
    }, ensure_ascii=False, indent=2))
    health = shadow.get_shadow_health(db_path=db_path)
    print(json.dumps({'health': health.as_dict()}, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Internal State Shadow admin')
    p.add_argument('--db', default=None, help='memories.db path')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('prepare-schema', help='create shadow tables only; no bootstrap')
    sub.add_parser('status', help='flags + health + proof gap')
    sub.add_parser('bootstrap', help='explicit production bootstrap')
    args = p.parse_args(argv)
    if args.cmd == 'prepare-schema':
        return _cmd_prepare_schema(args.db)
    if args.cmd == 'status':
        return _cmd_status(args.db)
    if args.cmd == 'bootstrap':
        return _cmd_bootstrap(args.db)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
