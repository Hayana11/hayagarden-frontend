#!/usr/bin/env python3.11
"""联想标签富化（记忆升级·方案二B）——每 6 小时 cron。

问题：召回（gateway._recall_memories / memory_tool.search_memories）是字面匹配，
"海边"想不起"沙滩上捡贝壳"。穷人版语义检索：写入后由便宜 LLM 给每条记忆生成
同义词/关联概念/实体别名，追加进 tags 列（`assoc:词1|词2|…` 段），召回时 tags
参与匹配，联想就接上了。

标记约定：tags 里含 `assoc:` 即已富化（没词可加时写 `assoc:-`），不重复处理。
不动 cleaner.py 已有的分类标签（日常/技术/色色/情绪/未完成），只追加。

用法：tag_enricher.py [--dry-run] [--limit N]
"""
import re
import sqlite3
import sys
import datetime

sys.path.insert(0, '/opt/frontend/tools')
import llm_lite

DB = '/opt/frontend/memories.db'
BATCH_LIMIT = 40          # 每次 cron 最多处理条数（增量，追得上写入速度即可）
MAX_ASSOC_WORDS = 8


def _log(msg):
    ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    print('[%s] %s' % (ts, msg), flush=True)


def _db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _clean_words(words, content):
    """LLM 返回的词清洗：去重、去空、去太长/太短、去已在正文出现的（正文里有的词
    LIKE 本来就能命中，写进 tags 是浪费）。"""
    seen, out = set(), []
    for w in words:
        w = re.sub(r'[\s"\'\[\]{}|,，、；;：:]+', '', str(w))
        if not (2 <= len(w) <= 12):
            continue
        if w in seen or w in content:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= MAX_ASSOC_WORDS:
            break
    return out


def enrich_one(content):
    """让 DeepSeek 产出联想词。返回 list[str]（可能为空）。"""
    prompt = (
        '下面是一条私人记忆。请给它生成"联想检索词"：当有人提起相近的话题时，'
        '这些词能帮系统想起这条记忆。\n'
        '要求：\n'
        '1. 输出同义词、上位/下位概念、强关联概念、实体别名；每个词 2-6 个字\n'
        '2. 不要输出记忆原文里已经出现过的词（原样出现的没有价值）\n'
        '3. 最多 8 个，宁缺毋滥；实在没有就输出空数组\n'
        '4. 只返回 JSON 数组，如 ["沙滩","海岸","度假"]，不要其他内容\n\n'
        '记忆内容：' + content[:600]
    )
    words = llm_lite.ask(prompt, max_tokens=200, expect_json=True)
    if not isinstance(words, list):
        return None  # API 失败，与"没词可加"区分开
    return _clean_words(words, content)


def run(dry_run=False, limit=BATCH_LIMIT):
    conn = _db()
    rows = conn.execute(
        "SELECT id, content, tags FROM posts "
        "WHERE resolved=0 AND (tags IS NULL OR tags NOT LIKE '%assoc:%') "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        conn.close()
        _log('nothing to enrich')
        return
    _log('%d rows to enrich%s' % (len(rows), ' (dry-run)' if dry_run else ''))
    done = failed = 0
    for r in rows:
        words = enrich_one(r['content'] or '')
        if words is None:
            failed += 1
            continue  # API 失败：不写标记，下次重试
        assoc = 'assoc:' + ('|'.join(words) if words else '-')
        new_tags = (r['tags'] + ',' + assoc) if r['tags'] else assoc
        _log('id=%d → %s' % (r['id'], assoc))
        if not dry_run:
            conn.execute("UPDATE posts SET tags=? WHERE id=?", (new_tags, r['id']))
            conn.commit()
        done += 1
    conn.close()
    _log('done: enriched=%d failed=%d' % (done, failed))


if __name__ == '__main__':
    _dry = '--dry-run' in sys.argv
    _lim = BATCH_LIMIT
    if '--limit' in sys.argv:
        try:
            _lim = int(sys.argv[sys.argv.index('--limit') + 1])
        except Exception:
            pass
    run(dry_run=_dry, limit=_lim)
