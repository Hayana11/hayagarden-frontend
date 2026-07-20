#!/usr/bin/env python3
"""B0-lite Wake preflight — zero model calls, zero spend.

Reports last-7-day wake stats, prompt block sizes, and which WAKE_TOOLS have
CC MCP twins. Used to decide the first CC Wake tool surface / cooling needs.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wake.cc_tools import (  # noqa: E402
    WAKE_TO_CC_MCP,
    cc_wake_allowed_tools,
    is_cc_wake_tool,
    relay_only_tool_names,
)


def _db_path(cli: str | None) -> str:
    return cli or os.environ.get('HAYAGARDEN_DB_PATH') or str(ROOT / 'memories.db')


def _load_wake_tool_names() -> list[str]:
    """Best-effort: import gateway.WAKE_TOOLS without starting Flask side effects."""
    try:
        # Prefer a light local list if gateway import is too heavy / missing deps.
        from codebase.client import CODEBASE_READ_TOOLS
        # Minimal calendar names matching gateway.CALENDAR_TOOLS
        calendar = [
            'get_todos', 'add_todo', 'get_countdowns',
            'get_ledger', 'add_ledger', 'get_ledger_budget',
        ]
        core = [
            'search_memories', 'get_location', 'get_device_status', 'get_light_status',
            'request_phone_screenshot', 'web_search', 'browse_github', 'read_webpage',
            'screenshot_chat', 'save_to_gallery', 'recall_photo', 'issue_command',
            'read_board', 'reply_to_board', 'post_to_board', 'get_activity_summary',
            'log_period_event', 'set_self_trigger', 'cancel_self_trigger',
            'get_wake_settings', 'set_wake_settings',
            'desire_add', 'desire_list', 'desire_act', 'desire_reflect', 'desire_history',
        ]
        return core + calendar + [t['name'] for t in CODEBASE_READ_TOOLS]
    except Exception as exc:
        return ['(failed to load tool names: %s)' % exc]


def _wake_stats(conn: sqlite3.Connection, days: int = 7) -> dict:
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(wake_log)')}
    except Exception:
        return {'error': 'wake_log missing'}
    if not cols:
        return {'error': 'wake_log missing'}

    since = (datetime.utcnow() + timedelta(hours=8) - timedelta(days=days)).strftime(
        '%Y-%m-%d %H:%M:%S'
    )
    where = "WHERE woke_at >= ?" if 'woke_at' in cols else ''
    params: tuple = (since,) if where else ()

    total = conn.execute(
        f'SELECT COUNT(*) FROM wake_log {where}', params
    ).fetchone()[0]

    action_col = 'action' if 'action' in cols else None
    actions = Counter()
    if action_col:
        for action, n in conn.execute(
            f'SELECT COALESCE(action,\"\"), COUNT(*) FROM wake_log {where} '
            f'GROUP BY COALESCE(action,\"\")',
            params,
        ):
            actions[str(action or '(empty)')] = int(n)

    message_sent = actions.get('message', 0)
    mode_dist: Counter = Counter()
    if 'cache_info' in cols:
        for (raw,) in conn.execute(
            f'SELECT cache_info FROM wake_log {where}', params
        ):
            mode = 'unknown'
            try:
                info = json.loads(raw or '{}')
                if isinstance(info, dict):
                    mode = str(info.get('mode') or info.get('wake_mode') or 'unknown')
            except Exception:
                pass
            mode_dist[mode] += 1

    # Consecutive call clusters: gaps ≤ 5 minutes.
    clusters = 0
    cluster_sizes: list[int] = []
    if 'woke_at' in cols:
        times = [
            row[0] for row in conn.execute(
                f'SELECT woke_at FROM wake_log {where} ORDER BY woke_at', params
            )
            if row[0]
        ]
        if times:
            clusters = 1
            size = 1
            prev = times[0]
            for ts in times[1:]:
                try:
                    a = datetime.strptime(str(prev)[:19], '%Y-%m-%d %H:%M:%S')
                    b = datetime.strptime(str(ts)[:19], '%Y-%m-%d %H:%M:%S')
                    gap = abs((b - a).total_seconds())
                except Exception:
                    gap = 9999
                if gap <= 300:
                    size += 1
                else:
                    cluster_sizes.append(size)
                    clusters += 1
                    size = 1
                prev = ts
            cluster_sizes.append(size)

    return {
        'days': days,
        'model_calls': int(total),
        'messages_sent': int(message_sent),
        'action_counts': dict(actions),
        'mode_dist': dict(mode_dist),
        'clusters': clusters,
        'cluster_sizes': cluster_sizes,
        'max_cluster': max(cluster_sizes) if cluster_sizes else 0,
    }


def _prompt_sizes() -> dict:
    out = {
        'stable_chars': None,
        'a1_chars': None,
        'wake_dynamic_chars': None,
        'note': '',
    }
    try:
        import config_store
        from chat.system_builder import build_system
        from wake.builder import append_system_text, build_prompt_suffix

        # Inspect sizes only — no model.
        system = build_system(wake=True, include_relationship_context=True)
        stable = 0
        dynamic = 0
        a1 = 0
        if isinstance(system, list):
            for block in system:
                if not isinstance(block, dict):
                    continue
                text = str(block.get('text') or '')
                n = len(text)
                if block.get('cache_control'):
                    stable += n
                else:
                    dynamic += n
                    if '关系上下文' in text or 'relationship' in text.lower() or '【关系' in text:
                        a1 += n
        else:
            stable = len(str(system or ''))

        suffix = build_prompt_suffix('normal', {
            'time': '2026-01-01 00:00',
            't2_hours': '1.0',
            't_hours': '1.0',
            'ritual_type': '',
            'activity_desc': '',
            'dream_tone': '',
            'dream_primer': '',
            'dream_tone_desc': '',
            'summary_date': '',
            'dialogue': '',
            'self_trigger_note': '',
        })
        wake_dyn = len(suffix) + dynamic
        out.update({
            'stable_chars': stable,
            'a1_chars': a1,
            'wake_dynamic_chars': wake_dyn,
            'rel_context_enabled': bool(
                config_store.get_bool('WAKE_RELATIONSHIP_CONTEXT_ENABLED', True)
            ),
        })
    except Exception as exc:
        out['note'] = 'prompt probe skipped: %s' % exc
    return out


def _tool_report(names: list[str]) -> dict:
    clean = [n for n in names if n and not n.startswith('(')]
    cc_open = sorted(n for n in clean if is_cc_wake_tool(n))
    relay_only = relay_only_tool_names(clean)
    return {
        'wake_tools_total': len(clean),
        'cc_mcp_open': cc_open,
        'cc_mcp_map': {k: WAKE_TO_CC_MCP[k] for k in cc_open if k in WAKE_TO_CC_MCP},
        'relay_only': relay_only,
        'cc_allowed_tools_csv': cc_wake_allowed_tools(
            [{'name': n} for n in cc_open]
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Wake B0-lite preflight (no model calls)')
    ap.add_argument('--db', default=None, help='memories.db path')
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    db = _db_path(args.db)
    stats = {'error': 'db missing: %s' % db}
    if os.path.exists(db):
        try:
            conn = sqlite3.connect(db, timeout=5)
            stats = _wake_stats(conn, days=args.days)
            conn.close()
        except Exception as exc:
            stats = {'error': str(exc)}

    prompt = _prompt_sizes()
    tools = _tool_report(_load_wake_tool_names())
    report = {'stats': stats, 'prompt': prompt, 'tools': tools}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print('过去 %d 天：' % args.days)
    if stats.get('error'):
        print('  (无法读取 wake_log: %s)' % stats['error'])
    else:
        print('  Wake 模型调用次数: %s' % stats.get('model_calls'))
        print('  真正发送消息次数: %s' % stats.get('messages_sent'))
        ac = stats.get('action_counts') or {}
        print(
            '  none / diary / explore 次数: %s / %s / %s'
            % (ac.get('none', 0), ac.get('diary', 0), ac.get('explore', 0))
        )
        print(
            '  连续调用簇: %s (最大簇 %s)'
            % (stats.get('clusters'), stats.get('max_cluster'))
        )
        print('  各 mode 分布: %s' % (stats.get('mode_dist') or {}))

    print()
    print('当前 Prompt：')
    print('  稳定块大小: %s' % prompt.get('stable_chars'))
    print('  A1 大小: %s' % prompt.get('a1_chars'))
    print('  Wake 动态块大小: %s' % prompt.get('wake_dynamic_chars'))
    if prompt.get('note'):
        print('  note: %s' % prompt['note'])

    print()
    print('工具：')
    print('  WAKE_TOOLS 总数: %s' % tools['wake_tools_total'])
    print('  有 CC MCP 的: %s' % (', '.join(tools['cc_mcp_open']) or '(none)'))
    print('  只能 Relay 使用: %s' % (', '.join(tools['relay_only']) or '(none)'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
