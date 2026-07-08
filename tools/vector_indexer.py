#!/usr/bin/env python3.11
"""向量索引器（记忆升级·方案二A）——每小时 cron。

EMBED_ENABLED 打开后：给 posts 里还没有向量（或模型换了）的记忆批量算 embedding，
存进 memory_vectors。没开就直接退出，零开销。全量补一遍后每小时只有增量。

用法：vector_indexer.py [--limit N]
"""
import datetime
import sqlite3
import sys

sys.path.insert(0, '/opt/frontend')
sys.path.insert(0, '/opt/frontend/tools')
import config_store
import embedding_tool

DB = '/opt/frontend/memories.db'
BATCH = 16
DEFAULT_LIMIT = 200


def _log(msg):
    ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    print('[%s] %s' % (ts, msg), flush=True)


def run(limit=DEFAULT_LIMIT):
    if not embedding_tool.enabled():
        _log('EMBED_ENABLED off, skip')
        return
    model = config_store.get('EMBED_MODEL', 'BAAI/bge-m3')
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT p.id, p.content FROM posts p "
        "LEFT JOIN memory_vectors v ON v.post_id = p.id AND v.model = ? "
        "WHERE v.post_id IS NULL AND length(p.content) >= 8 "
        "ORDER BY p.id DESC LIMIT ?", (model, limit)).fetchall()
    conn.close()
    if not rows:
        _log('index up to date')
        return
    _log('%d posts to embed (model=%s)' % (len(rows), model))
    ok = fail = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        vecs = embedding_tool.embed_texts([(r['content'] or '')[:1000] for r in batch])
        if not vecs:
            fail += len(batch)
            _log('batch %d failed' % (i // BATCH))
            continue
        for r, v in zip(batch, vecs):
            embedding_tool.store_vector(r['id'], v, model)
            ok += 1
    _log('done: ok=%d fail=%d' % (ok, fail))


if __name__ == '__main__':
    _lim = DEFAULT_LIMIT
    if '--limit' in sys.argv:
        try:
            _lim = int(sys.argv[sys.argv.index('--limit') + 1])
        except Exception:
            pass
    run(_lim)
