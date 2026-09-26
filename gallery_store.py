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
import hashlib
import pathlib
import secrets
import sqlite3

_DATA_ROOT = os.environ.get('HAYAGARDEN_GALLERY_ROOT', '/opt/frontend').strip() or '/opt/frontend'
DB_PATH = os.path.join(_DATA_ROOT, 'gallery.db')
GALLERY_DIR = os.path.join(_DATA_ROOT, 'gallery')


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
    # Additive-only migration.  Do not treat arbitrary SQLite errors as "column exists".
    for col, ddl in [
        ('summary',    "TEXT DEFAULT ''"),
        ('emotion',    "TEXT DEFAULT ''"),
        ('keywords',   "TEXT DEFAULT '[]'"),   # json 数组
        ('importance', 'INTEGER DEFAULT 0'),
        ('mem_id',     'INTEGER'),             # 关联的统一记忆(posts) id
        ('embedding',  'TEXT'),                # json 向量，第2b步再填
        ('last_sent_at', 'TEXT'),              # 上次被主动想起/发出的时间（第3步·避免反复发同一张）
        ('content_hash', 'TEXT'),
        ('visual_description', "TEXT DEFAULT ''"),
        ('first_impression', "TEXT DEFAULT ''"),
        ('send_count', 'INTEGER DEFAULT 0'),
        ('first_sent_at', 'TEXT'),
    ]:
        columns = {row[1] for row in c.execute('PRAGMA table_info(gallery_photos)').fetchall()}
        if col not in columns:
            try:
                c.execute('ALTER TABLE gallery_photos ADD COLUMN %s %s' % (col, ddl))
            except sqlite3.OperationalError as exc:
                # Concurrent app workers can both observe the missing column.
                # Ignore only that exact migration race; surface all other errors.
                if 'duplicate column name:' not in str(exc).lower():
                    raise
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS gallery_photos_content_hash_unique
                 ON gallery_photos(content_hash)
                 WHERE content_hash IS NOT NULL AND content_hash != '' ''')
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


def list_photos(album_id=None, limit=200, favorite_only=False):
    c = _conn()
    where, params = [], []
    if favorite_only:
        where.append('favorite=1')
    elif album_id:
        where.append('album_id=?'); params.append(album_id)
    sql = 'SELECT * FROM gallery_photos'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)
    rows = c.execute(sql, params).fetchall()
    c.close()
    return [dict(r) for r in rows]


def toggle_favorite(pid):
    """切换收藏(星标)。返回切换后的值(0/1)，pid 不存在返回 None。"""
    c = _conn()
    row = c.execute('SELECT favorite FROM gallery_photos WHERE pid=?', (pid,)).fetchone()
    if not row:
        c.close()
        return None
    nv = 0 if row['favorite'] else 1
    c.execute('UPDATE gallery_photos SET favorite=? WHERE pid=?', (nv, pid))
    c.commit()
    c.close()
    return nv


def update_photo(pid, note=None, album_id=None):
    """改备注 / 移动相册。只更新传了的字段。"""
    sets, vals = [], []
    if note is not None:
        sets.append('note=?'); vals.append(note)
    if album_id is not None:
        sets.append('album_id=?'); vals.append(album_id)
    if not sets:
        return False
    vals.append(pid)
    c = _conn()
    c.execute('UPDATE gallery_photos SET %s WHERE pid=?' % ','.join(sets), vals)
    c.commit()
    c.close()
    return True


def delete_photo(pid):
    """删除照片：删文件 + 删行。返回关联的 mem_id（调用方负责清理统一记忆），无则 None。"""
    c = _conn()
    row = c.execute('SELECT storage_key, mem_id FROM gallery_photos WHERE pid=?', (pid,)).fetchone()
    if not row:
        c.close()
        return None
    mem_id = row['mem_id']
    try:
        os.remove(os.path.join(GALLERY_DIR, row['storage_key']))
    except OSError:
        pass
    c.execute('DELETE FROM gallery_photos WHERE pid=?', (pid,))
    c.commit()
    c.close()
    return mem_id


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


def _extension_for_mime(mime):
    normalized = str(mime or '').strip().lower()
    if normalized == 'image/jpg':
        normalized = 'image/jpeg'
    extensions = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}
    if normalized not in extensions:
        raise ValueError('unsupported gallery image mime')
    return normalized, extensions[normalized]


def save_image_bytes(image_bytes, mime, note='', album_id=None, album_name=None,
                     source_type='chat', source_msg_id=None, source_chat_id=None,
                     created_at=None, first_impression=''):
    """Persist exact image bytes once; SQLite serializes duplicate claims."""
    data = bytes(image_bytes or b'')
    if not data:
        raise ValueError('empty gallery image')
    if len(data) > 20 * 1024 * 1024:
        raise ValueError('gallery image exceeds size limit')
    normalized_mime, ext = _extension_for_mime(mime)
    content_hash = hashlib.sha256(data).hexdigest()
    os.makedirs(GALLERY_DIR, exist_ok=True)
    c = _conn()
    dst = None
    try:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute(
            'SELECT pid, album_id FROM gallery_photos WHERE content_hash=? LIMIT 1',
            (content_hash,),
        ).fetchone()
        if existing:
            c.rollback()
            return {
                'pid': existing['pid'], 'album_id': existing['album_id'],
                'content_hash': content_hash, 'reused_existing': True,
            }

        # Rows written before R1 have no hash. Hash their permanent files lazily
        # so an exact re-save reuses the old pid instead of creating a second copy.
        legacy_rows = c.execute(
            "SELECT pid, album_id, storage_key FROM gallery_photos "
            "WHERE content_hash IS NULL OR content_hash='' ORDER BY id ASC"
        ).fetchall()
        root = pathlib.Path(GALLERY_DIR).resolve()
        for legacy in legacy_rows:
            key = str(legacy['storage_key'] or '')
            candidate = (root / key).resolve()
            try:
                if (candidate.is_symlink() or root not in candidate.parents
                        or not candidate.is_file()):
                    continue
                size = candidate.stat().st_size
                if size <= 0 or size > 20 * 1024 * 1024:
                    continue
                legacy_bytes = candidate.read_bytes()
            except OSError:
                continue
            legacy_hash = hashlib.sha256(legacy_bytes).hexdigest()
            try:
                c.execute('UPDATE gallery_photos SET content_hash=? WHERE pid=? '
                          "AND (content_hash IS NULL OR content_hash='')",
                          (legacy_hash, legacy['pid']))
            except sqlite3.IntegrityError:
                # Old databases may already contain duplicate files. Keep both
                # existing rows intact and leave the unique hash with its first row.
                if legacy_hash == content_hash:
                    match = c.execute(
                        'SELECT pid, album_id FROM gallery_photos WHERE content_hash=? LIMIT 1',
                        (content_hash,),
                    ).fetchone()
                    if match:
                        c.commit()
                        return {
                            'pid': match['pid'], 'album_id': match['album_id'],
                            'content_hash': content_hash, 'reused_existing': True,
                        }
                continue
            if legacy_hash == content_hash:
                c.commit()
                return {
                    'pid': legacy['pid'], 'album_id': legacy['album_id'],
                    'content_hash': content_hash, 'reused_existing': True,
                }

        if album_name:
            album = c.execute('SELECT id FROM albums WHERE name=? ORDER BY id ASC LIMIT 1',
                               (str(album_name).strip(),)).fetchone()
            if album:
                album_id = album['id']
            else:
                album_id = c.execute(
                    'INSERT INTO albums (name, description) VALUES (?,?)',
                    (str(album_name).strip(), ''),
                ).lastrowid
        if album_id is None:
            album = c.execute('SELECT id FROM albums ORDER BY id ASC LIMIT 1').fetchone()
            album_id = album['id'] if album else c.execute(
                'INSERT INTO albums (name, description) VALUES (?,?)',
                ('收藏', '随手存下的画面'),
            ).lastrowid

        pid = 'p' + secrets.token_hex(8)
        key = pid + ext
        dst = os.path.join(GALLERY_DIR, key)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as photo_file:
            photo_file.write(data)
            photo_file.flush()
            os.fsync(photo_file.fileno())
        width, height = _png_dims(dst)
        c.execute('''INSERT INTO gallery_photos
            (pid, album_id, storage_key, mime, width, height, note, source_type,
             source_msg_id, source_chat_id, created_at, saved_at, content_hash,
             visual_description, first_impression, send_count, first_sent_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (pid, album_id, key, normalized_mime, width, height, note or '', source_type,
             source_msg_id, source_chat_id, created_at, _now(), content_hash,
             '', str(first_impression or '').strip()[:800], 0, None))
        c.commit()
        return {
            'pid': pid, 'album_id': album_id, 'content_hash': content_hash,
            'reused_existing': False,
        }
    except Exception:
        c.rollback()
        if dst:
            try:
                os.remove(dst)
            except OSError:
                pass
        raise
    finally:
        c.close()


def save_attachment(attach_id, note='', album_id=None, album_name=None, source_type='chat',
                    source_msg_id=None, source_chat_id=None, first_impression=''):
    """Copy an existing temporary attachment into Gallery, preserving compatibility."""
    import attachment_store
    raw_ref = str(attach_id or '').strip()
    ref = raw_ref[len('attachment://'):] if raw_ref.startswith('attachment://') else raw_ref
    attachment = attachment_store.get(ref)
    if not attachment:
        return None
    try:
        from chat.cc_vision_bridge import resolve_image_bytes
        data, mime = resolve_image_bytes(
            'attachment://' + ref,
            attach_dir=getattr(attachment_store, 'ATTACH_DIR', '/opt/frontend/attachments'),
            get_attachment=attachment_store.get,
        )
    except Exception:
        return None
    return save_image_bytes(
        data, mime or attachment.get('mime') or 'image/png', note=note,
        album_id=album_id, album_name=album_name, source_type=source_type,
        source_msg_id=source_msg_id, source_chat_id=source_chat_id,
        created_at=attachment.get('created_at'), first_impression=first_impression,
    )


def save_from_attachment(attach_id, note='', album_id=None, source_type='chat',
                         source_msg_id=None, source_chat_id=None):
    """Legacy API: save an attachment and return its pid or None."""
    result = save_attachment(
        attach_id, note=note, album_id=album_id, source_type=source_type,
        source_msg_id=source_msg_id, source_chat_id=source_chat_id,
    )
    return result.get('pid') if result else None


def read_photo_bytes(pid, max_bytes=20 * 1024 * 1024):
    """Read a Gallery original without exposing or trusting storage_key paths."""
    row = get(pid)
    if not row:
        return None
    key = str(row.get('storage_key') or '')
    root = pathlib.Path(GALLERY_DIR).resolve()
    candidate = (root / key).resolve()
    try:
        if candidate.is_symlink() or root not in candidate.parents or not candidate.is_file():
            return None
        if candidate.stat().st_size <= 0 or candidate.stat().st_size > int(max_bytes):
            return None
        data = candidate.read_bytes()
    except OSError:
        return None
    if not data or len(data) > int(max_bytes):
        return None
    return data, row.get('mime') or 'image/png'


def set_meaning(pid, summary=None, emotion=None, keywords=None, importance=None,
                mem_id=None, embedding=None, visual_description=None,
                first_impression=None):
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
    if visual_description is not None:
        sets.append('visual_description=?'); vals.append(str(visual_description)[:1000])
    if first_impression is not None:
        sets.append('first_impression=?'); vals.append(str(first_impression)[:800])
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
    """Legacy recall-selection timestamp; provider delivery success is not available here."""
    c = _conn()
    c.execute("UPDATE gallery_photos SET last_sent_at=? WHERE pid=?", (_now(), pid))
    c.commit()
    c.close()


def mark_delivered(pid):
    """Record an actual successful image delivery when a caller has that evidence."""
    sent_at = _now()
    c = _conn()
    cur = c.execute('''UPDATE gallery_photos
        SET send_count=COALESCE(send_count,0)+1,
            first_sent_at=COALESCE(first_sent_at,?), last_sent_at=? WHERE pid=?''',
        (sent_at, sent_at, pid))
    c.commit()
    changed = cur.rowcount > 0
    c.close()
    return changed


def search_photos(q, limit=60):
    """按关键词搜照片（note/summary/keywords/视觉描述/第一印象 LIKE）。"""
    q = (q or '').strip()
    if not q:
        return list_photos(limit=limit)
    like = '%' + q + '%'
    c = _conn()
    rows = c.execute(
        'SELECT * FROM gallery_photos WHERE note LIKE ? OR summary LIKE ? OR keywords LIKE ? '
        'OR visual_description LIKE ? OR first_impression LIKE ? '
        'ORDER BY id DESC LIMIT ?', (like, like, like, like, like, limit)).fetchall()
    c.close()
    return [dict(r) for r in rows]


_init()
