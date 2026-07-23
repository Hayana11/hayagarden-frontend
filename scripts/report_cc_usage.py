#!/usr/bin/env python3
"""只读 Claude Code Usage 观测日报 CLI（阶段 1A）。

- SQLite read-only URI
- 不写数据库、不调网络、不调模型
- 与 GET /api/usage/cc-observability 共用聚合纯函数
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cc_usage_observability import (
    build_report_from_db,
    clamp_report_days,
    format_report_text,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Claude Code usage observability report (read-only)')
    parser.add_argument('--db', default='/opt/frontend/memories.db')
    parser.add_argument('--days', type=int, default=14, help='report window 1..90')
    parser.add_argument('--format', choices=('text', 'json'), default='text')
    parser.add_argument(
        '--now',
        help='optional ISO-8601 report clock for deterministic historical replay',
    )
    args = parser.parse_args(argv)

    days = clamp_report_days(args.days)
    report_now = None
    if args.now:
        raw_now = str(args.now).strip()
        if raw_now.endswith('Z'):
            raw_now = raw_now[:-1] + '+00:00'
        report_now = datetime.fromisoformat(raw_now)
    report = build_report_from_db(args.db, days=days, now=report_now)
    if args.format == 'json':
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(format_report_text(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
