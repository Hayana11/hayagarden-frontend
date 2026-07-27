#!/usr/bin/env python3
"""Generate a facts-only day handoff YAML for human review.

Read-only against memories.db; no model calls; no posts/diary writes.
Always prints the full YAML to stdout, then exits. Does not start shadow.

Chat day boundary: Asia/Shanghai 04:00:00 through next day 03:59:59.

Example:
  cd /opt/frontend
  python3 scripts/generate_day_handoff.py
  python3 scripts/generate_day_handoff.py --day 2026-07-26
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat.day_handoff import (
    build_day_handoff_from_messages,
    chat_day_str,
    fetch_day_messages,
    format_day_handoff_yaml,
    validate_day_handoff,
    validate_day_string,
    write_day_handoff_to_tmp,
)


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate day_handoff YAML for human review')
    parser.add_argument('--day', help='Chat day YYYY-MM-DD (default: previous chat day)')
    parser.add_argument('--json-summary', action='store_true', help='Also print JSON summary after YAML')
    args = parser.parse_args()

    from gateway import get_db

    day = validate_day_string(args.day or chat_day_str(offset_days=-1))
    rows = fetch_day_messages(get_db, day)
    data = build_day_handoff_from_messages(rows, day_str=day)
    errors = validate_day_handoff(data)
    if errors:
        print('validation failed:', '; '.join(errors), file=sys.stderr)
        return 2

    yaml_text = format_day_handoff_yaml(data)
    print(yaml_text, end='')

    try:
        path = write_day_handoff_to_tmp(data)
    except FileExistsError:
        path = '(not written: file already exists; review YAML above)'
    except Exception as exc:
        print('write failed:', exc, file=sys.stderr)
        return 3

    sha = hashlib.sha256(yaml_text.encode('utf-8')).hexdigest()
    if args.json_summary:
        print('\n--- summary ---')
        print(json.dumps({
            'ok': True,
            'path': path,
            'source_sha256': data.get('source_sha256'),
            'yaml_sha256': sha,
            'source_day': data.get('source_day'),
            'source_start_at': data.get('source_start_at'),
            'source_end_at': data.get('source_end_at'),
            'source_message_count': data.get('source_message_count'),
            'extraction_mode': data.get('extraction_mode'),
            'requires_human_review': data.get('requires_human_review'),
        }, ensure_ascii=False, indent=2), file=sys.stderr)
    else:
        print('\n# path: %s' % path, file=sys.stderr)
        print('# yaml_sha256: %s' % sha, file=sys.stderr)
        print('# source_sha256: %s' % data.get('source_sha256'), file=sys.stderr)
        print('# requires_human_review: true', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
