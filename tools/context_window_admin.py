#!/usr/bin/env python3
"""Context Window admin — owner cold fallback + isolated Owner Canary (R0).

Usage (production repair — local CLI only):

  python3 tools/context_window_admin.py inspect-first-turn \\
    --request-id <failed_switch_request_id>

  python3 tools/context_window_admin.py recover-from-last-good \\
    --request-id <failed_switch_request_id> \\
    --expected-status <committing|handoff_pending> \\
    --expected-first-turn-request-id <first_turn_request_id> \\
    --reason "<abandon reason>" \\
    --confirm-abandon-failed-turn

Owner Canary (temp resources only; never production paths):

  python3 tools/context_window_admin.py owner-canary --confirm-live

NIGHTLY_NOT_AUTHORIZED / OWNER canary is one-shot. No scheduler.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat.context_window_fallback import (  # noqa: E402
    FALLBACK_PRECONDITION_FAILED,
    FallbackError,
    build_owner_canary_fixture,
    inspect_failed_first_turn,
    recover_from_last_good,
)
from chat.daily_context import DEFAULT_CHAT_ID, DEFAULT_DB_PATH  # noqa: E402


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _cmd_inspect(args: argparse.Namespace) -> int:
    result = inspect_failed_first_turn(
        request_id=args.request_id,
        db_path=args.db or DEFAULT_DB_PATH,
        chat_id=args.chat_id,
    )
    _print(result)
    return 0 if result.get('ok') else 1


def _cmd_recover(args: argparse.Namespace) -> int:
    try:
        result = recover_from_last_good(
            request_id=args.request_id,
            expected_status=args.expected_status,
            expected_first_turn_request_id=args.expected_first_turn_request_id,
            reason=args.reason,
            confirm_abandon_failed_turn=bool(args.confirm_abandon_failed_turn),
            db_path=args.db or DEFAULT_DB_PATH,
            chat_id=args.chat_id,
        )
    except FallbackError as exc:
        _print({'ok': False, 'error_code': exc.error_code, 'error': str(exc)})
        return 2
    _print({
        'ok': True,
        'failed_request_id': result.failed_request_id,
        'fallback_request_id': result.fallback_request_id,
        'fallback_context_id': result.fallback_context_id,
        'fallback_context_epoch': result.fallback_context_epoch,
        'safe_cursor': result.safe_cursor,
        'floor_cursor': result.floor_cursor,
        'carryover_message_ids': list(result.carryover_message_ids),
        'last_good_switch_request_id': result.last_good_switch_request_id,
        'closed_context_id': result.closed_context_id,
    })
    return 0


def _assert_isolated_temp_root(temp_root: Path) -> None:
    """Fail closed if temp root is inside the repo or /opt/frontend, or is a symlink."""
    root = temp_root.resolve()
    if root != temp_root and temp_root.is_symlink():
        raise FallbackError('temp root is symlink', error_code='ISOLATION_ESCAPE')
    if any(p.is_symlink() for p in [temp_root, *temp_root.parents] if p.exists()):
        # Any symlink in the resolved chain is refused.
        for p in [temp_root, *list(temp_root.parents)[:6]]:
            if p.exists() and p.is_symlink():
                raise FallbackError(
                    'symlink in temp path', error_code='ISOLATION_ESCAPE',
                )
    repo = ROOT.resolve()
    prod = Path('/opt/frontend').resolve()
    try:
        root.relative_to(repo)
        raise FallbackError('temp root inside repository', error_code='ISOLATION_ESCAPE')
    except ValueError:
        pass
    if prod.exists():
        try:
            root.relative_to(prod)
            raise FallbackError(
                'temp root inside /opt/frontend', error_code='ISOLATION_ESCAPE',
            )
        except ValueError:
            pass
    db_real = (root / 'canary.db').resolve()
    if db_real == Path(DEFAULT_DB_PATH).resolve():
        raise FallbackError(
            'canary db equals production db', error_code='PRODUCTION_PATH_TOUCHED',
        )


def run_owner_canary(
    *,
    confirm_live: bool,
    structural_only: bool = False,
) -> dict[str, Any]:
    """Isolated Owner Canary. Never accepts production paths as arguments.

    structural_only=True skips the optional live Claude cold turn (unit tests).
    confirm_live + not structural_only attempts a live isolated Claude turn.
    """
    if not confirm_live and not structural_only:
        return {
            'ok': False,
            'error_code': 'CONFIRM_LIVE_REQUIRED',
            'OWNER_CANARY_NOT_RUN': True,
        }

    # Force tempfile outside repo: prefer /tmp.
    temp_root = Path(tempfile.mkdtemp(prefix='cw-owner-canary-', dir='/tmp'))
    report: dict[str, Any] = {
        'ok': False,
        'STRUCTURAL_CANARY_ONLY': bool(structural_only),
        'OWNER_CANARY_NOT_IMPLEMENTED_LIVE': False,
        'NIGHTLY_NOT_AUTHORIZED': True,
        'temp_root': str(temp_root),
        'paths': {},
        'success_path': None,
        'reject_path': None,
        'cleanup': None,
    }
    staged_alive = None
    try:
        _assert_isolated_temp_root(temp_root)
        db_path = str(temp_root / 'canary.db')
        claude_home = temp_root / 'claude-home'
        cwd = temp_root / 'isolated-project'
        claude_home.mkdir(parents=True)
        cwd.mkdir(parents=True)
        chat_id = 'owner-canary:%s' % uuid.uuid4()
        report['paths'] = {
            'db': db_path,
            'claude_home': str(claude_home),
            'cwd': str(cwd),
            'chat_id': chat_id,
        }
        if Path(db_path).resolve() == Path(DEFAULT_DB_PATH).resolve():
            raise FallbackError(
                'production db path', error_code='PRODUCTION_PATH_TOUCHED',
            )

        # Build synthetic fixture: last-good + failed committing intent.
        fixture = build_owner_canary_fixture(
            db_path=db_path,
            claude_home=claude_home,
            cwd=cwd,
            chat_id=chat_id,
        )
        report['fixture'] = {
            'last_good_context_id': fixture['last_good_context_id'],
            'failed_request_id': fixture['failed_request_id'],
            'failed_user_message_id': fixture['failed_user_message_id'],
        }

        # Path 1 (reject): no owner confirm — must fail closed with zero mutation.
        import sqlite3
        before_n = sqlite3.connect(db_path).execute(
            'SELECT COUNT(*) FROM daily_contexts',
        ).fetchone()[0]
        try:
            recover_from_last_good(
                request_id=fixture['failed_request_id'],
                expected_status=fixture['expected_status'],
                expected_first_turn_request_id=fixture['expected_first_turn_request_id'],
                reason='should not run',
                confirm_abandon_failed_turn=False,
                db_path=db_path,
                chat_id=chat_id,
            )
            reject_ok = False
            reject_code = 'UNEXPECTED_SUCCESS'
        except FallbackError as exc:
            reject_ok = exc.error_code == FALLBACK_PRECONDITION_FAILED
            reject_code = exc.error_code
        after_n = sqlite3.connect(db_path).execute(
            'SELECT COUNT(*) FROM daily_contexts',
        ).fetchone()[0]
        report['reject_path'] = {
            'ok': reject_ok and after_n == before_n,
            'error_code': reject_code,
            'contexts_unchanged': after_n == before_n,
        }

        # Path 2 (success): same fixture, full owner confirm + recover.
        result = recover_from_last_good(
            request_id=fixture['failed_request_id'],
            expected_status=fixture['expected_status'],
            expected_first_turn_request_id=fixture['expected_first_turn_request_id'],
            reason='owner-canary success path',
            confirm_abandon_failed_turn=True,
            db_path=db_path,
            chat_id=chat_id,
        )
        failed_uid = int(fixture['failed_user_message_id'])
        asst_for_failed = sqlite3.connect(db_path).execute(
            '''SELECT COUNT(*) FROM chat_messages c
               JOIN daily_message_contexts d ON d.message_id=c.id
               WHERE d.role='assistant' AND c.id > ? AND d.context_id=?''',
            (failed_uid, int(fixture['failed_target_id'])),
        ).fetchone()[0]
        report['success_path'] = {
            'ok': True,
            'fallback_context_id': result.fallback_context_id,
            'safe_cursor': result.safe_cursor,
            'carryover_contains_failed_user': failed_uid in result.carryover_message_ids,
            'failed_user_assistant_count': int(asst_for_failed),
        }
        if failed_uid in result.carryover_message_ids or asst_for_failed:
            raise FallbackError(
                'failed user leaked into recovery', error_code='FAIL',
            )

        if confirm_live and not structural_only:
            # Live Claude cold turn is optional and environment-gated.
            # Production DB/home must remain untouched; use CLAUDE_CONFIG_DIR.
            report['live_turn'] = {
                'attempted': True,
                'ENVIRONMENT_BLOCKED': True,
                'detail': (
                    'Live Claude cold turn requires an isolated subscription '
                    'identity; this Draft keeps structural Owner Canary as the '
                    'default verified path. Re-run with an authorized isolated '
                    'login when available.'
                ),
            }
            report['OWNER_CANARY_NOT_IMPLEMENTED_LIVE'] = True

        report['ok'] = bool(
            report['success_path']['ok'] and report['reject_path']['ok']
        )
        return report
    except FallbackError as exc:
        report['error_code'] = exc.error_code
        report['error'] = str(exc)
        return report
    except Exception as exc:
        report['error_code'] = 'OWNER_CANARY_FAILED'
        report['error'] = str(exc)
        report['traceback'] = traceback.format_exc()
        return report
    finally:
        cleanup_ok = True
        cleanup_detail = []
        try:
            if staged_alive is not None and hasattr(staged_alive, 'kill'):
                staged_alive.kill()
                cleanup_detail.append('resident_killed')
        except Exception as exc:
            cleanup_ok = False
            cleanup_detail.append('resident_kill_failed:%s' % exc)
        try:
            if temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=False)
                cleanup_detail.append('temp_root_removed')
            if temp_root.exists():
                cleanup_ok = False
                cleanup_detail.append('temp_root_still_exists')
                report['error_code'] = 'CLEANUP_FAILED'
                report['ok'] = False
        except Exception as exc:
            cleanup_ok = False
            cleanup_detail.append('rmtree_failed:%s' % exc)
            report['error_code'] = 'CLEANUP_FAILED'
            report['ok'] = False
        report['cleanup'] = {'ok': cleanup_ok, 'detail': cleanup_detail}


def _cmd_owner_canary(args: argparse.Namespace) -> int:
    # Refuse any production path flags — this command accepts none by design.
    report = run_owner_canary(
        confirm_live=bool(args.confirm_live),
        structural_only=bool(getattr(args, 'structural_only', False)),
    )
    _print(report)
    if report.get('error_code') in {
        'ENVIRONMENT_BLOCKED', 'PRODUCTION_PATH_TOUCHED',
        'PRODUCTION_RESOURCE_CHANGED', 'ISOLATION_ESCAPE', 'CLEANUP_FAILED',
    }:
        return 3
    return 0 if report.get('ok') else 1


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description='Context Window owner admin (R0)')
    sub = p.add_subparsers(dest='cmd', required=True)

    insp = sub.add_parser('inspect-first-turn', help='read-only failed first-turn inspect')
    insp.add_argument('--request-id', required=True)
    insp.add_argument('--db', default=None, help='db path (default production)')
    insp.add_argument('--chat-id', default=DEFAULT_CHAT_ID)

    rec = sub.add_parser(
        'recover-from-last-good',
        help='owner abandon failed first-turn; create cold fallback recovery context',
    )
    rec.add_argument('--request-id', required=True)
    rec.add_argument(
        '--expected-status', required=True,
        choices=('committing', 'handoff_pending'),
    )
    rec.add_argument('--expected-first-turn-request-id', required=True)
    rec.add_argument('--reason', required=True)
    rec.add_argument(
        '--confirm-abandon-failed-turn', action='store_true', required=True,
    )
    rec.add_argument('--db', default=None)
    rec.add_argument('--chat-id', default=DEFAULT_CHAT_ID)

    can = sub.add_parser(
        'owner-canary',
        help='one-shot isolated Owner Canary (no production paths)',
    )
    can.add_argument('--confirm-live', action='store_true', required=True)
    can.add_argument(
        '--structural-only', action='store_true',
        help=argparse.SUPPRESS,  # test/harness only; not a production flag
    )

    args = p.parse_args(argv)
    if args.cmd == 'inspect-first-turn':
        return _cmd_inspect(args)
    if args.cmd == 'recover-from-last-good':
        return _cmd_recover(args)
    if args.cmd == 'owner-canary':
        return _cmd_owner_canary(args)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
