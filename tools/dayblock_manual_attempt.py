#!/usr/bin/env python3
"""Manually retry or reconcile one non-ready DayBlock day.

retry          the day's latest job failed transiently and its source is unchanged;
               the same frozen job runs again with its frozen prompt, persona text
               and 03:00 authority.
reconciliation the day's source projection changed (for example after a bug fix);
               a new job is planned under the day's frozen 03:00 authority.

Ready days are final. A reason is required and is stored on the attempt.
Without --execute this is a dry run that makes no model call and writes no attempt.

Usage:
  python3 tools/dayblock_manual_attempt.py --day 2026-10-20 --reason "..." [--execute]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dayblock_scheduled_runner as runner  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--day', required=True, help='source day, YYYY-MM-DD')
    parser.add_argument('--reason', required=True, help='why this day is rerun (stored)')
    parser.add_argument('--execute', action='store_true', help='run it; default is a dry run')
    args = parser.parse_args(argv)
    outcome = runner.run_manual_attempt(
        source_day=args.day, reason=args.reason, execute=args.execute,
    )
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
    return 0 if outcome.get('status') in {'ready', 'dry_run'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
