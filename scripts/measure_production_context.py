#!/usr/bin/env python3
"""生产真实上下文测量（只读）。

读取 chat_messages.cache_info 中 CC resident 成功回复，按 turn_tags 分段汇报
各组件启发式估算与 provider 实测 cache/context。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cc_usage_observability import (
    build_report_from_db,
    classify_turn_tags,
    parse_cache_info_row,
)


COMPONENT_KEYS = (
    ('persona', 'persona_tokens_estimate'),
    ('stable_system', 'stable_note_tokens_estimate'),
    ('cold_once', 'cold_once_tokens_estimate'),
    ('state', 'state_tokens_estimate'),
    ('recall', 'memory_recall_tokens_estimate'),
    ('one_shot', 'one_shot_tokens_estimate'),
    ('wake_reply_bridge', 'wake_reply_bridge_tokens_estimate'),
    ('relationship', 'relationship_tokens_estimate'),
    ('history_bootstrap', 'history_bootstrap_tokens_estimate'),
    ('files', 'files_tokens_estimate'),
    ('tool_results', 'tool_result_tokens_estimate'),
    ('visible_payload', 'visible_payload_tokens_estimate'),
    ('provider_last_round_context', 'provider_last_round_context_tokens'),
    ('provider_cache_read', 'provider_turn_cache_read'),
    ('provider_cache_creation', 'provider_turn_cache_creation'),
)


def _avg(vals):
    clean = [float(v) for v in vals if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 1)


def build_measurement_report(db_path: str, *, days: int = 7) -> dict:
    base = build_report_from_db(db_path, days=days)
    rows = []
    from tools.cc_usage_observability import load_cache_info_rows, clamp_report_days
    for row in load_cache_info_rows(db_path, days=clamp_report_days(days)):
        kind, data = parse_cache_info_row(row.get('cache_info'))
        if kind != 'valid_v2':
            continue
        breakdown = data.get('context_breakdown') if isinstance(data.get('context_breakdown'), dict) else {}
        runtime = data.get('runtime') if isinstance(data.get('runtime'), dict) else {}
        tags = list(data.get('turn_tags') or breakdown.get('turn_tags') or [])
        if not tags:
            tags = classify_turn_tags(
                breakdown=breakdown,
                usage=data,
                runtime=runtime,
                is_cold=bool(runtime.get('is_cold')),
            )
        rows.append({
            'id': row.get('id'),
            'created_at': row.get('created_at'),
            'turn_tags': tags,
            'breakdown': breakdown,
            'turn_measurement': data.get('turn_measurement'),
            'resident_generation': runtime.get('resident_generation'),
        })

    by_tag = {}
    for item in rows:
        for tag in item['turn_tags']:
            bucket = by_tag.setdefault(tag, {k: [] for k, _ in COMPONENT_KEYS})
            bd = item['breakdown']
            for label, key in COMPONENT_KEYS:
                val = bd.get(key)
                if val is not None:
                    bucket[label].append(val)

    segments = {}
    for tag, buckets in sorted(by_tag.items()):
        segments[tag] = {
            'turn_count': sum(1 for item in rows if tag in item['turn_tags']),
            'averages': {label: _avg(buckets[label]) for label, _ in COMPONENT_KEYS},
        }

    return {
        'range': {'start': base.get('start_date'), 'end': base.get('end_date')},
        'summary': base.get('summary'),
        'turn_tag_segments': segments,
        'legacy_segments': base.get('turn_tag_segments'),
        'sample_turns': rows[-20:],
        'limitations': base.get('limitations'),
    }


def format_text(report: dict) -> str:
    lines = [
        '生产真实上下文测量',
        'range: %(start)s .. %(end)s' % report['range'],
        '',
    ]
    for tag, seg in (report.get('turn_tag_segments') or {}).items():
        lines.append('[%s] turns=%s' % (tag, seg.get('turn_count')))
        for label, val in (seg.get('averages') or {}).items():
            if val is not None:
                lines.append('  %s: %s' % (label, val))
        lines.append('')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Production context measurement (read-only)')
    parser.add_argument('--db', default='/opt/frontend/memories.db')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--format', choices=('text', 'json'), default='text')
    args = parser.parse_args(argv)
    report = build_measurement_report(args.db, days=args.days)
    if args.format == 'json':
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(format_text(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
