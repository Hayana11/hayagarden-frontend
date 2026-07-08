"""记忆系统唯一写入口（M1 统一协议）。
所有往 posts 表写记忆的代码——gateway 工具、[[SAVE:]]、日记、总结、梦、思绪、手动 API——
一律经过 save_memory()，不许裸 INSERT。schema 约定：
  type   : MEMORY / DIARY / DREAM / THOUGHT / DAILY_SUMMARY / WEEKLY_SUMMARY / MISS ...
  layer  : core（永久核心）/ long-term（长期）/ recent（近期，生命周期 cron 会代谢它）
  resolved: 0=活跃 1=已退役（自动注入和召回不再看它，主动搜索仍可见）
  recall_count / last_recalled_at: 召回加热（被真正注入 prompt 才算召回）
"""
import sqlite3
from tools import summary_title

DB_PATH = '/opt/frontend/memories.db'

VALID_LAYERS = ('core', 'long-term', 'recent')
RECALL_PROMOTE_LONG = 3
_DEDUP_TYPES = frozenset(('FACT', 'MEMORY', 'DIARY', 'THOUGHT', 'DAILY_SUMMARY'))


def normalize_content(text):
    """归一化正文，用于语义去重比较。"""
    import re
    t = re.sub(r'\s+', '', (text or '').strip())
    return re.sub(r'[^\w\u4e00-\u9fff]', '', t)


def _bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1)} if len(text) >= 2 else set()


def is_near_duplicate(a_norm, b_norm):
    """两条归一化正文是否应视为同一条记忆。"""
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    shorter, longer = (a_norm, b_norm) if len(a_norm) <= len(b_norm) else (b_norm, a_norm)
    if len(shorter) >= 12 and shorter in longer:
        return True
    n = min(24, len(a_norm), len(b_norm))
    if n >= 16 and a_norm[:n] == b_norm[:n]:
        return True
    if len(a_norm) >= 10 and len(b_norm) >= 10:
        ba, bb = _bigrams(a_norm), _bigrams(b_norm)
        if ba and bb:
            overlap = len(ba & bb) / min(len(ba), len(bb))
            if overlap >= 0.52:
                return True
    return False


def find_duplicate_id(conn, content, type='MEMORY'):
    """查找库内是否已有语义重复条目，返回 id 或 None。"""
    norm = normalize_content(content)
    if len(norm) < 10:
        return None
    if type in ('FACT', 'MEMORY'):
        type_filter = ('FACT', 'MEMORY')
    elif type in _DEDUP_TYPES:
        type_filter = (type,)
    else:
        return None
    placeholders = ','.join('?' * len(type_filter))
    rows = conn.execute(
        f"SELECT id, content FROM posts WHERE type IN ({placeholders}) "
        "AND COALESCE(resolved, 0) = 0 ORDER BY id DESC LIMIT 300",
        type_filter,
    ).fetchall()
    for row in rows:
        if is_near_duplicate(norm, normalize_content(row['content'])):
            return int(row['id'])
    return None


def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def save_memory(content, type='MEMORY', author='fyodor', layer='recent',
                tags='', importance=0, pinned=0, created_at=None, processed=None):
    """统一写入口。created_at 传 None 用表默认（东八现在）。返回新 id。

    processed=None → 默认 0（待夜巡判定）；FACT 抽取等可传 1。
    """
    content = (content or '').strip()
    if not content:
        return None
    if layer not in VALID_LAYERS:
        layer = 'recent'
    if processed is None:
        processed = 1 if type == 'FACT' else 0
    else:
        processed = int(processed)
    conn = _db()
    dup_id = find_duplicate_id(conn, content, type)
    if dup_id is not None:
        conn.close()
        return dup_id
    post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
    has_summary_title = 'summary_title' in post_cols
    has_processed = 'processed' in post_cols
    gen_title = summary_title.generate_summary_title(content)
    cols = ['type', 'content', 'author', 'layer', 'tags', 'importance', 'pinned']
    vals = [type, content, author, layer, tags, int(importance), int(pinned)]
    if has_processed:
        cols.append('processed')
        vals.append(processed)
    if created_at:
        cols.append('created_at')
        vals.append(created_at)
    if has_summary_title:
        cols.append('summary_title')
        vals.append(gen_title)
    placeholders = ','.join('?' * len(cols))
    cur = conn.execute(
        f"INSERT INTO posts ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def _recall_promote_threshold():
    try:
        import config_store as cs
        return cs.get_int('RECALL_PROMOTE_LONG', RECALL_PROMOTE_LONG)
    except Exception:
        return RECALL_PROMOTE_LONG


def touch_memories(ids):
    """召回加热：这些记忆刚被注入了 prompt。kiwi-mem 的定义——被写进上下文才算真的被想起。"""
    if not ids:
        return
    threshold = _recall_promote_threshold()
    conn = _db()
    conn.executemany(
        "UPDATE posts SET recall_count = COALESCE(recall_count,0) + 1, "
        "last_recalled_at = datetime('now','+8 hours') WHERE id = ?",
        [(i,) for i in ids])
    for mid in ids:
        row = conn.execute(
            "SELECT recall_count, layer FROM posts WHERE id=?", (mid,)).fetchone()
        if not row:
            continue
        rc = int(row['recall_count'] or 0)
        layer = (row['layer'] or 'recent').strip()
        if rc >= threshold and layer == 'recent':
            conn.execute(
                "UPDATE posts SET layer='long-term' WHERE id=? AND layer='recent'",
                (mid,))
    conn.commit()
    conn.close()


def get_recent_memories(limit=20):
    conn = _db()
    rows = conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_memories(keyword, active_only=False):
    """多词检索：空格/逗号分隔的词先 AND，无结果退化为 OR 按命中数排序。
    tags 参与匹配（tag_enricher 写入的联想词 assoc:… 由此生效——"海边"搜得到"沙滩"）。
    主动搜索默认包含已退役记忆（active_only=False）——历史该搜得到；自动注入路径传 True。"""
    words = [w for w in keyword.replace('，', ' ').replace(',', ' ').split() if w]
    if not words:
        return []
    resolved_cond = ' AND resolved=0' if active_only else ''
    conn = _db()
    hay = "(content || ' ' || COALESCE(tags,''))"
    params = tuple('%' + w + '%' for w in words)
    cond_and = ' AND '.join([hay + ' LIKE ?'] * len(words))
    rows = conn.execute(
        f"SELECT * FROM posts WHERE {cond_and}{resolved_cond} ORDER BY pinned DESC, id DESC LIMIT 20",
        params).fetchall()
    if not rows and len(words) > 1:
        cond_or = ' OR '.join([hay + ' LIKE ?'] * len(words))
        hits = '+'.join(['(' + hay + ' LIKE ?)'] * len(words))
        rows = conn.execute(
            f"SELECT *, ({hits}) AS _hits FROM posts WHERE ({cond_or}){resolved_cond} "
            f"ORDER BY _hits DESC, pinned DESC, id DESC LIMIT 20",
            params + params).fetchall()
    conn.close()
    return [dict(r) for r in rows]
