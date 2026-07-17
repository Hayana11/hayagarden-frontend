#!/usr/bin/env python3.11
"""一次性：按四层语义纠正已有 posts 的 type/layer/importance。

用法：python3 rescore_memory_tiers.py [--dry-run]
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, '/opt/frontend/tools')
sys.path.insert(0, '/opt/frontend')

from memory_tier import (
    compute_display_weight,
    has_deep_emotional,
    has_stable_couple_fact,
    is_ephemeral_note,
)

DB = '/opt/frontend/memories.db'
LIBRARY_TYPES = ('MEMORY', 'DIARY', 'FACT', 'THOUGHT', 'DREAM', 'DAILY_SUMMARY')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT id, type, content, layer, importance, processed, tags, recall_count, pinned
            FROM posts WHERE type IN ({','.join('?' * len(LIBRARY_TYPES))})
            AND COALESCE(resolved, 0) = 0""",
        LIBRARY_TYPES,
    ).fetchall()

    changed = 0
    for row in rows:
        content = (row['content'] or '').strip()
        ptype = (row['type'] or 'MEMORY').strip()
        layer = (row['layer'] or 'recent').strip()
        imp = int(row['importance'] or 0)
        processed = int(row['processed'] or 0)
        tags = row['tags'] or ''

        new_type, new_layer, new_imp = ptype, layer, imp

        if is_ephemeral_note(content) and ptype == 'FACT':
            new_type = 'MEMORY'
            new_layer = 'recent'
            new_imp = min(imp, 4) if imp else 3
            if new_imp < 3:
                new_imp = 3

        elif has_stable_couple_fact(content) and not is_ephemeral_note(content):
            if imp < 8:
                new_imp = 8
            new_layer = 'core'
            if ptype not in ('FACT', 'MEMORY'):
                new_type = 'MEMORY'

        elif has_deep_emotional(content, tags) and imp >= 7 and layer != 'core':
            new_layer = 'core'
            if imp < 8:
                new_imp = 8

        if not processed and new_imp >= 3:
            pass  # 不擅自标 processed，留给夜巡

        if (new_type, new_layer, new_imp) == (ptype, layer, imp):
            continue

        w_before = compute_display_weight(row)
        preview = {
            'id': row['id'],
            'before': f'{ptype}/{layer}/imp={imp}/w={w_before}',
            'after': f'{new_type}/{new_layer}/imp={new_imp}',
            'snippet': content[:48],
        }
        print(preview)
        if not args.dry_run:
            conn.execute(
                'UPDATE posts SET type=?, layer=?, importance=? WHERE id=?',
                (new_type, new_layer, new_imp, row['id']),
            )
        changed += 1

    if not args.dry_run:
        conn.commit()
    conn.close()
    print('done: %d rows %s' % (changed, 'preview' if args.dry_run else 'updated'))


if __name__ == '__main__':
    main()
