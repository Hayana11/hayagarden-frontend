"""Gallery（收藏相册）—— 永久存档层。

与临时的 attachment 层解耦：
- attachment 是易失的(7天/30张自动删)；gallery 是永久的。
- "收藏" = 把一张临时 attachment 的文件**复制**进 gallery（各留各的），
  得到永久句柄 pid，对外用 gallery://<pid> 引用，只经 /api/gallery/photo/<pid> 取。
- 独立 gallery.db，不碰 memories.db。照片的"意义"(summary/emotion) 留给后续
  的记忆层(统一 posts 记忆, type=PHOTO)，这里只管文件与元数据。
- storage_key 存的是本地相对文件名；将来要换对象存储，只改取图实现、schema 不动。
"""
import os
import struct
import shutil
import secrets
import sqlite3

DB_PATH     = '/opt/frontend/gallery.db'
GALLERY_DIR = '/opt/frontend/gallery'


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _now():
    import datetime
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')


def _init():
    os.makedirs(GALLERY_DIR, exist_ok=True)
    c = _conn()
    c.execute('''CREATE TABLE IF NOT EXISTS albums (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        cover_pid   TEXT,
        created_at  DATETIME DEFAULT (datetime('now','+8 hours')),
        updated_at  DATETIME DEFAULT (datetime('now','+8 hours'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS gallery_photos (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        pid            TEXT UNIQUE NOT NULL,
        album_id       INTEGER,
        storage_key    TEXT NOT NULL,
        mime           TEXT DEFAULT 'image/png',
        width          INTEGER,
        height         INTEGER,
        note           TEXT DEFAULT '',
        source_type    TEXT DEFAULT '',
        source_msg_id  INTEGER,
        source_chat_id INTEGER,
        tags           TEXT DEFAULT '[]',
        favorite       INTEGER DEFAULT 0,
        created_at     TEXT,
        saved_at       DATETIME DEFAULT (datetime('now','+8 hours'))
    )''')
    # 第2步·照片记忆的意义字段（gallery.db 是本模块自己的库，安全 ALTER）
    for col, ddl in [
        ('summary',    "TEXT DEFAULT ''"),
        ('emotion',    "TEXT DEFAULT ''"),
        ('keywords',   "TEXT DEFAULT '[]'"),   # json 数组
        ('importance', 'INTEGER DEFAULT 0'),
        ('mem_id',     'INTEGER'),             # 关联的统一记忆(posts) id
        ('embedding',  'TEXT'),                # json 向量，第2b步再填
        ('last_sent_at', 'TEXT'),              # 上次被主动想起/发出的时间（第3步·避免反复发同一张）
    ]:
        try:
            c.execute('ALTER TABLE gallery_photos ADD COLUMN %s %s' % (col, ddl))
        except sqlite3.OperationalError:
            pass  # 列已存在
    # 确保有一本默认相册（直接原生 SQL，不走 create_album，避免 _init 自引用递归）
    n = c.execute('SELECT COUNT(*) FROM albums').fetchone()[0]
    if n == 0:
        c.execute('INSERT INTO albums (name, description) VALUES (?,?)', ('收藏', '随手存下的画面'))
    c.commit()
    c.close()


def _default_album_id():
    try:
        c = _conn()
        row = c.execute('SELECT id FROM albums ORDER BY id ASC LIMIT 1').fetchone()
        c.close()
        return row['id'] if row else None
    except Exception:
        return None


def create_album(name, description=''):
    c = _conn()
    cur = c.execute('INSERT INTO albums (name, description) VALUES (?,?)', (name, description))
    aid = cur.lastrowid
    c.commit()
    c.close()
    return aid


def album_by_name(name):
    c = _conn()
    row = c.execute('SELECT id FROM albums WHERE name=? ORDER BY id ASC LIMIT 1', (name,)).fetchone()
    c.close()
    return row['id'] if row else None


def list_albums():
    c = _conn()
    rows = c.execute('''SELECT a.*, (SELECT COUNT(*) FROM gallery_photos p WHERE p.album_id=a.id) AS n
                        FROM albums a ORDER BY a.id ASC''').fetchall()
    c.close()
    return [dict(r) for r in rows]


def list_photos(album_id=None, limit=200):
    c = _conn()
    if album_id:
        rows = c.execute('SELECT * FROM gallery_photos WHERE album_id=? ORDER BY id DESC LIMIT ?',
                         (album_id, limit)).fetchall()
    else:
        rows = c.execute('SELECT * FROM gallery_photos ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get(pid):
    try:
        c = _conn()
        row = c.execute('SELECT * FROM gallery_photos WHERE pid=?', (pid,)).fetchone()
        c.close()
        return dict(row) if row else None
    except Exception:
        return None


def _png_dims(path):
    """无依赖读 PNG 宽高（IHDR）。非 PNG 返回 (None, None)。"""
    try:
        with open(path, 'rb') as f:
            head = f.read(24)
        if head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR':
            w, h = struct.unpack('>II', head[16:24])
            return w, h
    except Exception:
        pass
    return None, None


def save_from_attachment(attach_id, note='', album_id=None, source_type='chat',
                         source_msg_id=None, source_chat_id=None):
    """把一张临时 attachment 复制进 gallery，永久保存，返回 pid。
    attachment 可能之后过期，但 gallery 有自己的副本。"""
    import attachment_store
    ref = (attach_id or '').strip()
    if ref.startswith('attachment://'):
        ref = ref[len('attachment://'):]
    a = attachment_store.get(ref)
    if not a:
        return None
    src = os.path.join(attachment_store.ATTACH_DIR, a['filename'])
    if not os.path.exists(src):
        return None
    pid = 'p' + secrets.token_hex(4)
    ext = os.path.splitext(a['filename'])[1] or '.png'
    key = pid + ext
    dst = os.path.join(GALLERY_DIR, key)
    shutil.copy2(src, dst)  # 复制，不移动
    w, h = _png_dims(dst)
    if album_id is None:
        album_id = _default_album_id()
    c = _conn()
    c.execute('''INSERT INTO gallery_photos
        (pid, album_id, storage_key, mime, width, height, note, source_type,
         source_msg_id, source_chat_id, created_at, saved_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (pid, album_id, key, a.get('mime', 'image/png'), w, h, note, source_type,
         source_msg_id, source_chat_id, a.get('created_at'), _now()))
    c.commit()
    c.close()
    return pid


def set_meaning(pid, summary=None, emotion=None, keywords=None, importance=None,
                mem_id=None, embedding=None):
    """写入照片的"意义"（第2步·照片记忆）。只更新传了的字段。keywords/embedding 传 list。"""
    import json as _json
    sets, vals = [], []
    if summary is not None:
        sets.append('summary=?'); vals.append(summary)
    if emotion is not None:
        sets.append('emotion=?'); vals.append(emotion)
    if keywords is not None:
        sets.append('keywords=?'); vals.append(_json.dumps(keywords, ensure_ascii=False))
    if importance is not None:
        sets.append('importance=?'); vals.append(int(importance))
    if mem_id is not None:
        sets.append('mem_id=?'); vals.append(int(mem_id))
    if embedding is not None:
        sets.append('embedding=?'); vals.append(_json.dumps(embedding))
    if not sets:
        return False
    vals.append(pid)
    c = _conn()
    c.execute('UPDATE gallery_photos SET %s WHERE pid=?' % ','.join(sets), vals)
    c.commit()
    c.close()
    return True


def count_photos():
    try:
        c = _conn()
        n = c.execute('SELECT COUNT(*) FROM gallery_photos').fetchone()[0]
        c.close()
        return n
    except Exception:
        return 0


def pick_for_recall(keyword=None, emotion=None):
    """第3步·主动回忆：挑一张"值得突然想起"的照片。
    偏好：有意义(summary)、importance 高、最近没发过；可按关键词/情绪过滤。
    在前几名里加权随机，避免每次都是同一张。返回 dict 或 None。"""
    import random
    c = _conn()
    rows = c.execute("SELECT * FROM gallery_photos WHERE summary IS NOT NULL AND summary != ''").fetchall()
    c.close()
    cands = [dict(r) for r in rows]
    if not cands:
        return None
    kw = (keyword or '').strip()
    emo = (emotion or '').strip()
    if kw:
        cands = [p for p in cands if kw in (p.get('summary') or '') or kw in (p.get('keywords') or '') or kw in (p.get('note') or '')] or cands
    if emo:
        cands = [p for p in cands if emo in (p.get('emotion') or '')] or cands

    def score(p):
        s = (p.get('importance') or 0)
        if not p.get('last_sent_at'):
            s += 40          # 从没发过的加分
        return s
    cands.sort(key=score, reverse=True)
    top = cands[:5]
    weights = [score(p) + 1 for p in top]
    return random.choices(top, weights=weights, k=1)[0]


def mark_sent(pid):
    c = _conn()
    c.execute("UPDATE gallery_photos SET last_sent_at=? WHERE pid=?", (_now(), pid))
    c.commit()
    c.close()


def search_photos(q, limit=60):
    """按关键词搜照片（note/summary/keywords 里 LIKE）。第2b步再加 embedding 语义搜。"""
    q = (q or '').strip()
    if not q:
        return list_photos(limit=limit)
    like = '%' + q + '%'
    c = _conn()
    rows = c.execute(
        'SELECT * FROM gallery_photos WHERE note LIKE ? OR summary LIKE ? OR keywords LIKE ? '
        'ORDER BY id DESC LIMIT ?', (like, like, like, limit)).fetchall()
    c.close()
    return [dict(r) for r in rows]


_init()
