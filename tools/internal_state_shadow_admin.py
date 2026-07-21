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
  python3 tools/internal_state_shadow_admin.py ack-gap --incident-id I --reason '...'
  python3 tools/internal_state_shadow_admin.py inspect-quarantine
  python3 tools/internal_state_shadow_admin.py reconcile-quarantine \\
      --path ... --sha256 ... --reason '...'

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
            'quarantine_reconcile': _table_exists(
                conn, shadow.QUARANTINE_RECONCILE_TABLE,
            ),
            'proof_gap': shadow.has_unresolved_proof_gap(conn, db_path=path),
            'unresolved_incidents': shadow.count_unresolved_gap_incidents(conn),
            'bootstrapped': False,
            'capture_alert_configured': shadow.capture_alert_configured(),
            'capture_alert_pending': shadow.has_capture_alert(path),
            'capture_alert_preflight': shadow.capture_alert_preflight(path),
            'user_events_preflight_ok': (
                (not shadow.is_user_events_enabled())
                or (
                    shadow.outbox_schema_ready(conn)
                    and shadow.score_proof_schema_ready(conn)
                    and not shadow.has_unresolved_proof_gap(conn, db_path=path)
                    and shadow.capture_alert_configured()
                    and shadow.capture_alert_preflight(path)['ok']
                    and not shadow.has_capture_alert(path)
                    and shadow.count_incomplete_recovery_intents(conn) == 0
                )
            ),
        }
        st = store.read_state(conn)
        ready['bootstrapped'] = st is not None
        print(json.dumps(ready, ensure_ascii=False, indent=2))
        if shadow.is_user_events_enabled() and not ready['user_events_preflight_ok']:
            return 1
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
        events_on = shadow.is_user_events_enabled(environ=environ)
        alert_preflight = shadow.capture_alert_preflight(path)
        preflight_ok = (
            (not events_on)
            or (
                shadow.outbox_schema_ready(conn)
                and shadow.score_proof_schema_ready(conn)
                and not shadow.has_unresolved_proof_gap(conn, db_path=path)
                and shadow.capture_alert_configured()
                and alert_preflight['ok']
                and not shadow.has_capture_alert(path)
                and shadow.count_incomplete_recovery_intents(conn) == 0
            )
        )
        payload = {
            'flags': {
                'SHADOW_ENABLED': shadow.is_shadow_enabled(environ=environ),
                'SCORE_PROOF_ENABLED': shadow.is_score_proof_enabled(environ=environ),
                'USER_EVENTS_ENABLED': events_on,
                'USER_EVENTS_FLAG_RAW': (
                    str(environ.get(shadow.USER_EVENTS_ENABLED_ENV, '0')).strip() == '1'
                ),
            },
            'health': health.as_dict(),
            'proof_health': proof.as_dict(),
            'unresolved_incidents': shadow.list_unresolved_gap_incidents(conn),
            'gap_sidecar_pending': shadow.count_gap_sidecar_pending(path),
            'quarantine_pending': shadow.count_quarantine_pending(path),
            'quarantine_files': shadow.list_gap_quarantine_files(path),
            'capture_evidence_failures_process_local': shadow.capture_evidence_failure_count(),
            'capture_alert_configured': shadow.capture_alert_configured(),
            'capture_alert_pending': shadow.has_capture_alert(path),
            'capture_alert_preflight': alert_preflight,
            'recovery_intents_pending': shadow.count_incomplete_recovery_intents(conn),
            'user_events_preflight_ok': preflight_ok,
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
        or (health.quarantine_pending or 0) > 0
        or bool(health.capture_alert_pending)
        or not payload['user_events_preflight_ok']
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
            'quarantine_pending': shadow.count_quarantine_pending(path),
            'quarantine_files': shadow.list_gap_quarantine_files(path),
            'recent_acks': acks,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not payload['unresolved'] else 1
    finally:
        conn.close()


def _cmd_ack_gap(
    db_path: str,
    message_id: int | None,
    reason: str,
    incident_id: int | None,
) -> int:
    if message_id is None and incident_id is None:
        print('ERROR: require --message-id or --incident-id', file=sys.stderr)
        return 2
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


def _cmd_inspect_quarantine(db_path: str) -> int:
    items = shadow.inspect_quarantine(db_path)
    print(json.dumps({
        'quarantine_pending': len(items),
        'items': items,
    }, ensure_ascii=False, indent=2))
    return 0 if not items else 1


def _cmd_reconcile_quarantine(
    db_path: str,
    path: str,
    sha256: str,
    reason: str,
    backfill_event_key: str | None,
    incident_id: int | None,
) -> int:
    db = isv3.memories_db_path(db_path)
    conn = store.open_store(db)
    try:
        shadow.ensure_shadow_schema(conn, db_path=db)
        result = shadow.reconcile_quarantine(
            conn,
            path=path,
            sha256=sha256,
            reason=reason,
            db_path=db,
            backfill_event_key=backfill_event_key,
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


def _cmd_recover_quarantine_intents(db_path: str) -> int:
    db = isv3.memories_db_path(db_path)
    conn = store.open_store(db)
    try:
        shadow.ensure_shadow_schema(conn, db_path=db)
        if conn.in_transaction:
            conn.execute('COMMIT')
        result = shadow.recover_quarantine_intents(conn, db_path=db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(x.get('reconciled') for x in result) else 1
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_pending_incidents(
    db_path: str, path: str | None, action: str | None,
    reason: str | None, stale_after_seconds: int,
) -> int:
    db = isv3.memories_db_path(db_path)
    conn = store.open_store(db)
    try:
        shadow.ensure_shadow_schema(conn, db_path=db)
        if action is None:
            result = shadow.inspect_pending_incidents(
                db, stale_after_seconds=stale_after_seconds,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if not result else 1
        if path is None or reason is None:
            print('ERROR: --path and --reason required with --action', file=sys.stderr)
            return 2
        result = shadow.recover_pending_incident_tmp(
            conn, path=path, action=action, reason=reason,
            stale_after_seconds=stale_after_seconds, db_path=db,
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


def _cmd_ack_capture_alert(db_path: str, sha256: str, reason: str) -> int:
    db = isv3.memories_db_path(db_path)
    conn = store.open_store(db)
    try:
        result = shadow.ack_capture_alert(
            conn, sha256=sha256, reason=reason, db_path=db,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_recover_pending_incident_intents(db_path: str) -> int:
    db = isv3.memories_db_path(db_path)
    conn = store.open_store(db)
    try:
        shadow.ensure_shadow_schema(conn, db_path=db)
        if conn.in_transaction:
            conn.execute('COMMIT')
        result = shadow.recover_pending_incident_intents(conn, db_path=db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(x.get('status') == 'completed' for x in result) else 1
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_capture_alert_acks(db_path: str, recover: bool) -> int:
    db = isv3.memories_db_path(db_path)
    conn = store.open_store(db)
    try:
        result = (
            shadow.recover_capture_alert_acks(conn)
            if recover else shadow.inspect_capture_alert_acks(conn)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if (recover and all(x.get('acked') for x in result)) else (0 if not result else 1)
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
        help='audited resolve of gap incident(s); NULL mid needs --incident-id only',
    )
    ack_p.add_argument('--message-id', type=int, default=None)
    ack_p.add_argument('--reason', required=True)
    ack_p.add_argument('--incident-id', type=int, default=None)
    sub.add_parser('inspect-quarantine', help='list quarantine files + sha256')
    rq = sub.add_parser(
        'reconcile-quarantine',
        help='audited archive + resolve one quarantine file',
    )
    rq.add_argument('--path', required=True)
    rq.add_argument('--sha256', required=True)
    rq.add_argument('--reason', required=True)
    rq.add_argument('--backfill-event-key', default=None)
    rq.add_argument('--incident-id', type=int, default=None)
    sub.add_parser(
        'recover-quarantine-intents',
        help='finalize prepared reconcile intents after a crash',
    )
    pi = sub.add_parser(
        'inspect-pending-incidents',
        help='list or audited-recover stale per-file incident tmp',
    )
    pi.add_argument('--stale-after-seconds', type=int, default=300)
    pi.add_argument('--path', default=None)
    pi.add_argument('--action', choices=('promote', 'quarantine', 'discard'))
    pi.add_argument('--reason', default=None)
    aca = sub.add_parser(
        'ack-capture-alert',
        help='audited archive of configured independent capture alert marker',
    )
    aca.add_argument('--sha256', required=True)
    aca.add_argument('--reason', required=True)
    sub.add_parser(
        'recover-pending-incident-intents',
        help='complete prepared stale-tmp action intents after a crash',
    )
    sub.add_parser('inspect-capture-alert-acks', help='show prepared capture alert acks')
    sub.add_parser('recover-capture-alert-acks', help='resume prepared capture alert acks')
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
    if args.cmd == 'inspect-quarantine':
        return _cmd_inspect_quarantine(args.db)
    if args.cmd == 'reconcile-quarantine':
        return _cmd_reconcile_quarantine(
            args.db,
            args.path,
            args.sha256,
            args.reason,
            args.backfill_event_key,
            args.incident_id,
        )
    if args.cmd == 'recover-quarantine-intents':
        return _cmd_recover_quarantine_intents(args.db)
    if args.cmd == 'inspect-pending-incidents':
        return _cmd_pending_incidents(
            args.db, args.path, args.action, args.reason,
            args.stale_after_seconds,
        )
    if args.cmd == 'ack-capture-alert':
        return _cmd_ack_capture_alert(args.db, args.sha256, args.reason)
    if args.cmd == 'recover-pending-incident-intents':
        return _cmd_recover_pending_incident_intents(args.db)
    if args.cmd == 'inspect-capture-alert-acks':
        return _cmd_capture_alert_acks(args.db, False)
    if args.cmd == 'recover-capture-alert-acks':
        return _cmd_capture_alert_acks(args.db, True)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
