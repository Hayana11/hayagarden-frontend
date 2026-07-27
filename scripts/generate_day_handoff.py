#!/usr/bin/env python3
"""Generate a facts-only day handoff YAML under /tmp from chat_messages.

Read-only against memories.db; no model calls; no posts/diary writes.

Example:
  cd /opt/frontend
  python3 scripts/generate_day_handoff.py
  python3 scripts/generate_day_handoff.py --day 2026-07-26
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat.day_handoff import (
    build_and_write_yesterday_handoff,
    build_day_handoff_from_messages,
    calendar_day_str,
    fetch_day_messages,
    format_day_handoff_yaml,
    validate_day_handoff,
    write_day_handoff_to_tmp,
)


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate /tmp day_handoff YAML')
    parser.add_argument('--day', help='Calendar day YYYY-MM-DD in UTC+8 (default: yesterday)')
    parser.add_argument('--stdout', action='store_true', help='Print YAML to stdout instead of summary JSON')
    args = parser.parse_args()

    from gateway import get_db

    day = args.day or calendar_day_str(offset_days=-1)
    rows = fetch_day_messages(get_db, day)
    data = build_day_handoff_from_messages(rows, day_str=day)
    errors = validate_day_handoff(data)
    if errors:
        print('validation failed:', '; '.join(errors), file=sys.stderr)
        return 2
    path = write_day_handoff_to_tmp(data)
    if args.stdout:
        print(format_day_handoff_yaml(data), end='')
        return 0
    print(json.dumps({
        'ok': True,
        'path': path,
        'day': day,
        'message_count': data.get('message_count'),
        'user_message_count': data.get('user_message_count'),
        'validation_errors': errors,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
