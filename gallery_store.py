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
    c.commit()
    c.close()
    # 确保有一本默认相册
    if _default_album_id() is None:
        create_album('收藏', '随手存下的画面')


_ALL_INIT = False


def _ensure():
    global _ALL_INIT
    if not _ALL_INIT:
        _init()
        _ALL_INIT = True


def _default_album_id():
    try:
        c = _conn()
        row = c.execute('SELECT id FROM albums ORDER BY id ASC LIMIT 1').fetchone()
        c.close()
        return row['id'] if row else None
    except Exception:
        return None


def create_album(name, description=''):
    _ensure()
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
    _ensure()
    c = _conn()
    rows = c.execute('''SELECT a.*, (SELECT COUNT(*) FROM gallery_photos p WHERE p.album_id=a.id) AS n
                        FROM albums a ORDER BY a.id ASC''').fetchall()
    c.close()
    return [dict(r) for r in rows]


def list_photos(album_id=None, limit=200):
    _ensure()
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
    _ensure()
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


_init()
