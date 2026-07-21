#!/usr/bin/env python3
"""Internal State v3 Shadow 管理 CLI（Phase 1A-4b）

显式 schema / bootstrap / status / outbox drain / gap 审计恢复。
禁止在评分事务内偷偷建表。

用法：
  python3 tools/internal_state_shadow_admin.py prepare-schema
  python3 tools/internal_state_shadow_admin.py status
  python3 tools/internal_state_shadow_admin.py bootstrap
  python3 tools/internal_state_shadow_admin.py drain-outbox
  python3 tools/internal_state_shadow_admin.py inspect-gap
  python3 tools/internal_state_shadow_admin.py ack-gap --message-id N --reason '...'
  python3 tools/internal_state_shadow_admin.py ack-gap --incident-id N --message-id M --reason '...'

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
        shadow.ensure_shadow_schema(conn, db_path=path)
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
            'gap_incidents': _table_exists(conn, shadow.GAP_INCIDENTS_TABLE),
            'outbox': shadow.outbox_schema_ready(conn),
            'gap_ack': _table_exists(conn, shadow.GAP_ACK_TABLE),
            'proof_gap': shadow.has_unresolved_proof_gap(conn, db_path=path),
            'unresolved_incidents': shadow.count_unresolved_gap_incidents(conn),
            'bootstrapped': False,
        }
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
        path = isv3.memories_db_path(db_path)
        proof = shadow.read_proof_health(conn)
        wm = None
        if shadow.score_proof_schema_ready(conn):
            try:
                wm = shadow.resolve_scored_watermark(conn)
            except Exception as exc:
                wm = f'error:{exc}'
        pending = (
            shadow.count_pending_outbox(conn)
            if shadow.outbox_schema_ready(conn) else None
        )
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
            'unresolved_incidents': shadow.list_unresolved_gap_incidents(conn),
            'gap_sidecar_pending': shadow.count_gap_sidecar_pending(path),
            'proof_max_message_id': wm,
            'outbox_pending': pending,
            'watermark_lag': health.watermark_lag,
        }
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if (
        health.proof_gap
        or health.watermark_lag
        or (pending or 0) > 0
        or (health.gap_sidecar_pending or 0) > 0
    ):
        return 1
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


def _cmd_drain_outbox(db_path: str, limit: int) -> int:
    summary = shadow.drain_shadow_outbox(db_path=db_path, limit=limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary.get('skipped_disabled'):
        return 2
    if summary.get('failed'):
        return 1
    return 0


def _cmd_inspect_gap(db_path: str) -> int:
    path = isv3.memories_db_path(db_path)
    conn = store.open_store(path)
    try:
        proof = shadow.read_proof_health(conn)
        acks = []
        if _table_exists(conn, shadow.GAP_ACK_TABLE):
            rows = conn.execute(
                f"""
                SELECT id, incident_id, message_id, reason, acked_at,
                       previous_error_code, previous_failed_message_id
                FROM {shadow.GAP_ACK_TABLE}
                ORDER BY id DESC LIMIT 20
                """
            ).fetchall()
            for r in rows:
                if hasattr(r, 'keys'):
                    acks.append(dict(r))
                else:
                    acks.append({
                        'id': r[0],
                        'incident_id': r[1],
                        'message_id': r[2],
                        'reason': r[3],
                        'acked_at': r[4],
                        'previous_error_code': r[5],
                        'previous_failed_message_id': r[6],
                    })
        payload = {
            'unresolved': shadow.has_unresolved_proof_gap(conn, db_path=path),
            'proof_health': proof.as_dict(),
            'incidents': shadow.list_unresolved_gap_incidents(conn),
            'gap_sidecar_pending': shadow.count_gap_sidecar_pending(path),
            'recent_acks': acks,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not payload['unresolved'] else 1
    finally:
        conn.close()


def _cmd_ack_gap(
    db_path: str,
    message_id: int,
    reason: str,
    incident_id: int | None,
) -> int:
    path = isv3.memories_db_path(db_path)
    conn = store.open_store(path)
    try:
        shadow.ensure_shadow_schema(conn, db_path=path)
        result = shadow.ack_proof_gap(
            conn,
            message_id=message_id,
            reason=reason,
            db_path=path,
            incident_id=incident_id,
        )
        if conn.in_transaction:
            conn.execute('COMMIT')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Internal State Shadow admin')
    p.add_argument('--db', default=None, help='memories.db path')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('prepare-schema', help='create shadow tables only; no bootstrap')
    sub.add_parser('status', help='flags + health + proof gap + watermark lag')
    sub.add_parser('bootstrap', help='explicit production bootstrap')
    drain_p = sub.add_parser('drain-outbox', help='deliver pending shadow outbox rows')
    drain_p.add_argument('--limit', type=int, default=32)
    sub.add_parser('inspect-gap', help='show durable proof gap incidents + recent acks')
    ack_p = sub.add_parser(
        'ack-gap',
        help='audited resolve of gap incident(s) for one message_id',
    )
    ack_p.add_argument('--message-id', type=int, required=True)
    ack_p.add_argument('--reason', required=True)
    ack_p.add_argument('--incident-id', type=int, default=None)
    args = p.parse_args(argv)
    if args.cmd == 'prepare-schema':
        return _cmd_prepare_schema(args.db)
    if args.cmd == 'status':
        return _cmd_status(args.db)
    if args.cmd == 'bootstrap':
        return _cmd_bootstrap(args.db)
    if args.cmd == 'drain-outbox':
        return _cmd_drain_outbox(args.db, args.limit)
    if args.cmd == 'inspect-gap':
        return _cmd_inspect_gap(args.db)
    if args.cmd == 'ack-gap':
        return _cmd_ack_gap(args.db, args.message_id, args.reason, args.incident_id)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
