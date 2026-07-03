"""记忆系统唯一写入口（M1 统一协议）。
所有往 posts 表写记忆的代码——gateway 工具、[[SAVE:]]、日记、总结、梦、思绪、手动 API——
一律经过 save_memory()，不许裸 INSERT。schema 约定：
  type   : MEMORY / DIARY / DREAM / THOUGHT / DAILY_SUMMARY / WEEKLY_SUMMARY / MISS ...
  layer  : core（永久核心）/ long-term（长期）/ recent（近期，生命周期 cron 会代谢它）
  resolved: 0=活跃 1=已退役（自动注入和召回不再看它，主动搜索仍可见）
  recall_count / last_recalled_at: 召回加热（被真正注入 prompt 才算召回）
"""
import sqlite3
DB_PATH = '/opt/frontend/memories.db'

VALID_LAYERS = ('core', 'long-term', 'recent')


def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def save_memory(content, type='MEMORY', author='fyodor', layer='recent',
                tags='', importance=0, pinned=0, created_at=None):
    """统一写入口。created_at 传 None 用表默认（东八现在）。返回新 id。"""
    content = (content or '').strip()
    if not content:
        return None
    if layer not in VALID_LAYERS:
        layer = 'recent'
    conn = _db()
    if created_at:
        cur = conn.execute(
            "INSERT INTO posts (type, content, author, layer, tags, importance, pinned, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (type, content, author, layer, tags, int(importance), int(pinned), created_at))
    else:
        cur = conn.execute(
            "INSERT INTO posts (type, content, author, layer, tags, importance, pinned) "
            "VALUES (?,?,?,?,?,?,?)",
            (type, content, author, layer, tags, int(importance), int(pinned)))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def touch_memories(ids):
    """召回加热：这些记忆刚被注入了 prompt。kiwi-mem 的定义——被写进上下文才算真的被想起。"""
    if not ids:
        return
    conn = _db()
    conn.executemany(
        "UPDATE posts SET recall_count = COALESCE(recall_count,0) + 1, "
        "last_recalled_at = datetime('now','+8 hours') WHERE id = ?",
        [(i,) for i in ids])
    conn.commit()
    conn.close()


def get_recent_memories(limit=20):
    conn = _db()
    rows = conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_memories(keyword, active_only=False):
    """多词检索：空格/逗号分隔的词先 AND，无结果退化为 OR 按命中数排序。
    主动搜索默认包含已退役记忆（active_only=False）——历史该搜得到；自动注入路径传 True。"""
    words = [w for w in keyword.replace('，', ' ').replace(',', ' ').split() if w]
    if not words:
        return []
    resolved_cond = ' AND resolved=0' if active_only else ''
    conn = _db()
    params = tuple('%' + w + '%' for w in words)
    cond_and = ' AND '.join(['content LIKE ?'] * len(words))
    rows = conn.execute(
        f"SELECT * FROM posts WHERE {cond_and}{resolved_cond} ORDER BY pinned DESC, id DESC LIMIT 20",
        params).fetchall()
    if not rows and len(words) > 1:
        cond_or = ' OR '.join(['content LIKE ?'] * len(words))
        hits = '+'.join(['(content LIKE ?)'] * len(words))
        rows = conn.execute(
            f"SELECT *, ({hits}) AS _hits FROM posts WHERE ({cond_or}){resolved_cond} "
            f"ORDER BY _hits DESC, pinned DESC, id DESC LIMIT 20",
            params + params).fetchall()
    conn.close()
    return [dict(r) for r in rows]
