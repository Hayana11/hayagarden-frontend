import sqlite3
DB_PATH = '/opt/frontend/memories.db'

def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def save_memory(content, type='MEMORY', author='fyodor'):
    conn = _db()
    conn.execute("INSERT INTO posts (type,content,author) VALUES (?,?,?)", (type, content, author))
    conn.commit()
    conn.close()

def get_recent_memories(limit=20):
    conn = _db()
    rows = conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def search_memories(keyword):
    """多词检索：空格/逗号分隔的词先 AND，无结果退化为 OR 按命中数排序。
    （旧版把整句当一个字符串 LIKE，"向日葵 圣诞"这种分开出现的永远搜不到）"""
    words = [w for w in keyword.replace('，', ' ').replace(',', ' ').split() if w]
    if not words:
        return []
    conn = _db()
    params = tuple('%' + w + '%' for w in words)
    cond_and = ' AND '.join(['content LIKE ?'] * len(words))
    rows = conn.execute(
        f"SELECT * FROM posts WHERE {cond_and} ORDER BY pinned DESC, id DESC LIMIT 20",
        params).fetchall()
    if not rows and len(words) > 1:
        cond_or = ' OR '.join(['content LIKE ?'] * len(words))
        hits = '+'.join(['(content LIKE ?)'] * len(words))
        rows = conn.execute(
            f"SELECT *, ({hits}) AS _hits FROM posts WHERE {cond_or} "
            f"ORDER BY _hits DESC, pinned DESC, id DESC LIMIT 20",
            params + params).fetchall()
    conn.close()
    return [dict(r) for r in rows]
