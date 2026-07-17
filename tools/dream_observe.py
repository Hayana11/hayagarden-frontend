#!/usr/bin/env python3.11
"""
梦境生成观测脚本（第三批）——只读统计，不写规则、不打分。

依赖第一批写入的 dream_pool.metadata，回答：
  现实锚点命中率 / 释梦结尾命中率 / 重生成率及原因分布
  素材复述（primer_copy）率 / source_mix 长期分布 / 母题使用频次

用法：
  python3 tools/dream_observe.py
  python3 tools/dream_observe.py --limit 50
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter

DB_PATH = '/opt/frontend/memories.db'


def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _parse_meta(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def observe(limit=100):
    conn = _db()
    cols = [r[1] for r in conn.execute('PRAGMA table_info(dream_pool)').fetchall()]
    if 'metadata' not in cols:
        print('dream_pool.metadata 列不存在——第一批迁移尚未生效')
        conn.close()
        return 1

    rows = conn.execute(
        """
        SELECT id, tone, created_at, metadata, length(content) AS clen
        FROM dream_pool
        WHERE metadata IS NOT NULL AND TRIM(metadata) != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        print(f'最近 {limit} 条中无带 metadata 的梦——尚无 v3 样本')
        conn.close()
        return 0

    n = len(rows)
    regen = 0
    fallback = 0
    fail_reasons = Counter()
    traits = Counter()
    mix_sums = Counter()
    mix_keys = ('recent', 'remote', 'diary', 'wake_log', 'synthetic', 'old_motif')
    prompt_versions = Counter()

    for r in rows:
        meta = _parse_meta(r['metadata'])
        if not meta:
            continue
        prompt_versions[meta.get('prompt_version') or '?'] += 1
        if meta.get('regenerated'):
            regen += 1
        if meta.get('fallback'):
            fallback += 1
        fr = meta.get('failure_reason')
        if fr:
            fail_reasons[fr] += 1
        for t in meta.get('traits') or []:
            traits[t] += 1
        sm = meta.get('source_mix') or {}
        for k in mix_keys:
            mix_sums[k] += int(sm.get(k) or 0)

    print(f'=== 梦境观测（最近 {n} 场带 metadata 的梦）===')
    print(f'prompt_version: {dict(prompt_versions)}')
    print(f'regenerated: {regen}/{n} ({100*regen/n:.1f}%)')
    print(f'fallback:    {fallback}/{n} ({100*fallback/n:.1f}%)')
    print('failure_reason 分布（入库时仍失败 / 触发过重生的最终原因）:')
    if fail_reasons:
        for k, v in fail_reasons.most_common():
            print(f'  {k}: {v} ({100*v/n:.1f}%)')
    else:
        print('  （无）')
    print('关键现实锚点/释梦/复述的信号：看 failure_reason 里的')
    print('  reality_anchor / explanatory_closure / primer_copy')
    print('  ——若日摘感回升而命中为零，说明正则清单需更新（模型习惯变了）')

    print('traits 出现频次:')
    for k, v in traits.most_common():
        print(f'  {k}: {v} ({100*v/n:.1f}%)')

    print('source_mix 场均:')
    for k in mix_keys:
        print(f'  {k}: {mix_sums[k]/n:.2f}')

    # 母题频次（若第二批表存在）
    try:
        motifs = conn.execute(
            """
            SELECT content, origin, recurrence, last_used_at
            FROM dream_latents
            ORDER BY recurrence DESC, id DESC
            LIMIT 15
            """
        ).fetchall()
        print('dream_latents top recurrence:')
        if motifs:
            for m in motifs:
                print(
                    f"  [{m['origin']}] rec={m['recurrence']} "
                    f"last={m['last_used_at'] or '-'} | {m['content'][:40]}"
                )
        else:
            print('  （空）')
    except sqlite3.OperationalError:
        print('dream_latents 表不存在（第二批未上）')

    # 与最近梦的粗相似度：连续 18 字重叠对数（观测用，不做拒绝）
    contents = conn.execute(
        """
        SELECT id, content FROM dream_pool
        WHERE content IS NOT NULL AND TRIM(content) != ''
        ORDER BY id DESC LIMIT ?
        """,
        (min(limit, 30),),
    ).fetchall()
    overlap_hits = 0
    pairs = 0
    for i in range(len(contents) - 1):
        a = (contents[i]['content'] or '').replace(' ', '')
        b = (contents[i + 1]['content'] or '').replace(' ', '')
        pairs += 1
        hit = False
        if len(a) >= 18 and len(b) >= 18:
            for j in range(0, len(a) - 17):
                if a[j:j + 18] in b:
                    hit = True
                    break
        if hit:
            overlap_hits += 1
    if pairs:
        print(f'相邻梦 18字连续重叠: {overlap_hits}/{pairs} ({100*overlap_hits/pairs:.1f}%)')

    conn.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description='梦境生成观测（只读）')
    ap.add_argument('--limit', type=int, default=100)
    args = ap.parse_args()
    sys.exit(observe(limit=args.limit))


if __name__ == '__main__':
    main()
