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
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM posts WHERE content LIKE ? ORDER BY id DESC LIMIT 20",
        ('%' + keyword + '%',)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
