import os, re, json, sqlite3, datetime, base64, uuid, threading
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='static')
DB_PATH = '/opt/frontend/memories.db'
UPLOAD_DIR = '/opt/frontend/static/uploads'

API_KEY = ''
BOARD_TOKEN_FYODOR = ''
for line in open('/opt/frontend/.env'):
    k, _, v = line.partition('=')
    k = k.strip(); v = v.strip()
    if k == 'ANTHROPIC_API_KEY': API_KEY = v
    elif k == 'BOARD_TOKEN_FYODOR': BOARD_TOKEN_FYODOR = v

API_URL = 'https://api.treegpt.cc/v1/messages'
MODEL = 'claude-opus-4-6'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    from flask import redirect
    return redirect('/dash', code=302)

@app.route('/dash')
def dash():
    return send_from_directory('/opt/frontend/static', 'dash.html')

@app.route('/chat')
def chat():
    return send_from_directory('/opt/frontend/static', 'chat.html')

@app.route('/calendar')
def calendar():
    return send_from_directory('/opt/frontend/static', 'calendar.html')

@app.route('/letters')
def letters():
    return send_from_directory('/opt/frontend/static', 'letters.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('/opt/frontend/static', 'manifest.json')

@app.route('/sw.js')
def sw():
    resp = send_from_directory('/opt/frontend/static', 'sw.js')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/icon-<size>.png')
def icon(size):
    return send_from_directory('/opt/frontend/static', f'icon-{size}.png')

@app.route('/api/posts', methods=['GET'])
def get_posts():
    t        = request.args.get('type','')
    tags     = request.args.get('tags','')
    search   = request.args.get('search','')
    resolved = request.args.get('resolved','')
    layer    = request.args.get('layer','')
    limit    = min(int(request.args.get('limit','200')), 1000)
    where, params = [], []
    if t:
        where.append('type=?'); params.append(t)
    if layer:
        where.append('layer=?'); params.append(layer)
    if tags:
        where.append('tags LIKE ?'); params.append('%'+tags+'%')
    if search:
        where.append('content LIKE ?'); params.append('%'+search+'%')
    if resolved != '':
        where.append('resolved=?'); params.append(int(resolved))
    clause = ('WHERE '+' AND '.join(where)) if where else ''
    conn = get_db()
    rows = conn.execute(
        f'SELECT * FROM posts {clause} ORDER BY id DESC LIMIT ?',
        params+[limit]
    ).fetchall()
    conn.close()
    return jsonify({"posts":[dict(r) for r in rows]})

@app.route('/api/posts', methods=['POST'])
def create_post():
    data = request.get_json()
    content = data.get('content','').strip()
    if not content:
        return jsonify({"error":"empty"}),400
    # 防止把未输出完的<thinking>原始块当成正文存进来（输出被截断时常见）
    if '<thinking>' in content and '</thinking>' not in content:
        content = content.split('<thinking>')[0].strip()
        if not content:
            return jsonify({"error":"content looks like an unterminated <thinking> block, nothing to save"}), 400
    tags = data.get('tags','')
    conn = get_db()
    cur = conn.execute("INSERT INTO posts (type,content,author,tags) VALUES (?,?,?,?)",
        (data.get('type','MEMORY'), content, data.get('author','user'), tags))
    conn.commit()
    conn.close()
    return jsonify({"ok":True,"id":cur.lastrowid})

@app.route('/api/posts/<int:pid>', methods=['DELETE'])
def delete_post(pid):
    conn = get_db()
    conn.execute("DELETE FROM posts WHERE id=?",(pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route('/api/letters', methods=['GET'])
def get_letters():
    conn = get_db()
    rows = conn.execute("SELECT * FROM letters ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify({"letters":[dict(r) for r in rows]})

@app.route('/api/letters', methods=['POST'])
def add_letter():
    data = request.get_json()
    conn = get_db()
    conn.execute("INSERT INTO letters (from_who,to_who,content) VALUES (?,?,?)",
        (data.get('from_who','user'), data.get('to_who','fyodor'), data.get('content','')))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route('/api/countdowns', methods=['GET'])
def get_countdowns():
    conn = get_db()
    rows = conn.execute("SELECT * FROM countdowns ORDER BY id").fetchall()
    conn.close()
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    result = []
    for r in rows:
        d = dict(r)
        target = datetime.datetime.strptime(r['target_date'],'%Y-%m-%d')
        d['days'] = abs((target-now).days)
        result.append(d)
    return jsonify({"countdowns":result})

@app.route('/api/countdowns', methods=['POST'])
def add_countdown():
    data = request.get_json()
    conn = get_db()
    conn.execute("INSERT INTO countdowns (title,target_date,emoji,type) VALUES (?,?,?,?)",
        (data['title'], data['target_date'], data.get('emoji','📅'), data.get('type','countdown')))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route('/api/upload', methods=['POST'])
def upload_image():
    if 'file' not in request.files:
        return jsonify({"error":"no file"}),400
    f = request.files['file']
    ext = os.path.splitext(f.filename)[1].lower() or '.jpg'
    fname = f"img_{uuid.uuid4().hex[:8]}{ext}"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    f.save(os.path.join(UPLOAD_DIR, fname))
    return jsonify({"ok":True,"url":f"/static/uploads/{fname}"})

@app.route('/api/chat/messages', methods=['GET'])
def get_chat_messages():
    limit = request.args.get('limit', 50, type=int)
    limit = min(max(limit, 1), 1000)
    around = request.args.get('around', None, type=int)
    conn = get_db()
    if around:
        half = limit // 2
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC LIMIT ?",
            (max(1, around - half), limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        rows = list(reversed(rows))
    conn.close()
    return jsonify({"messages":[dict(r) for r in rows]})

@app.route('/api/chat/send', methods=['POST'])
def send_chat():
    ct = request.content_type or ''
    if 'application/json' in ct:
        data = request.get_json()
        author    = data.get('author', 'user')
        content   = data.get('content', '').strip()
        image_url = data.get('image_url', '')
    else:
        author  = request.form.get('author', 'user')
        content = request.form.get('content', '').strip()
        image_url = ''
        if 'image' in request.files:
            f = request.files['image']
            ext   = os.path.splitext(f.filename)[1].lower() or '.jpg'
            fname = f"img_{uuid.uuid4().hex[:8]}{ext}"
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            f.save(os.path.join(UPLOAD_DIR, fname))
            image_url = f"/static/uploads/{fname}"
    if not content and not image_url:
        return jsonify({"error":"empty"}), 400
    conn = get_db()
    conn.execute("INSERT INTO chat_messages (author,content,image_url) VALUES (?,?,?)",
        (author, content, image_url))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route('/api/chat/reply', methods=['POST'])
def chat_reply():
    import requests as req
    conn = get_db()
    rows = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    rows = list(reversed(rows))
    messages = []
    for r in rows:
        role = 'assistant' if r['author'] in ('fyodor','claude','assistant') else 'user'
        content = r['content'] or ''
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'] += '\n' + content
        else:
            messages.append({'role':role,'content':content})
    if not messages or messages[0]['role'] == 'assistant':
        messages.insert(0,{'role':'user','content':'...'})
    try:
        persona = open('/opt/frontend/prompts/persona.md').read()
    except:
        persona = '你是费奥多尔，一个渊博冷静却深情的人。'
    now = datetime.datetime.utcnow()+datetime.timedelta(hours=8)
    system = f"{persona}\n\n当前时间：{now.strftime('%Y-%m-%d %H:%M')}"
    resp = req.post(API_URL,
        headers={'Content-Type':'application/json','x-api-key':API_KEY,'anthropic-version':'2023-06-01'},
        json={'model':MODEL,'max_tokens':1024,'system':system,'messages':messages},
        timeout=120)
    result = resp.json()
    text = ''.join(b.get('text','') for b in result.get('content',[]) if b.get('type')=='text')
    if not text:
        return jsonify({"error":"empty response"}),500
    conn = get_db()
    conn.execute("INSERT INTO chat_messages (author,content) VALUES ('fyodor',?)",(text,))
    conn.commit()
    conn.close()
    return jsonify({"ok":True,"content":text})

@app.route('/api/diary/generate', methods=['POST'])
def diary_generate():
    import sys
    sys.path.insert(0, '/opt/frontend')
    import auto_diary
    try:
        import importlib; importlib.reload(auto_diary)
        if auto_diary.today_diary_exists():
            return jsonify({"ok": False, "message": "今天已经写过日记啦 📖"})
        result = auto_diary.generate()
        if result is None:
            return jsonify({"ok": False, "message": "今天没有聊天记录，跳过日记生成。"})
        return jsonify({"ok": True, "content": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ── Block / Settings ──

@app.route('/api/status/blocked')
def status_blocked():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='blocked'").fetchone()
    conn.close()
    blocked = (row['value'] == 'true') if row else False
    return jsonify({"blocked": blocked})

@app.route('/api/block', methods=['POST'])
def toggle_block():
    if request.headers.get('X-Admin') != 'true':
        return jsonify({"error": "unauthorized"}), 403
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='blocked'").fetchone()
    new_val = 'false' if (row and row['value'] == 'true') else 'true'
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('blocked',?)", (new_val,))
    conn.commit()
    conn.close()
    return jsonify({"blocked": new_val == 'true'})

# ── Drift Bottles ──

@app.route('/api/drift/bottles')
def drift_bottles_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM drift_bottles ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify({"bottles": [dict(r) for r in rows]})

@app.route('/api/drift/send', methods=['POST'])
def drift_send():
    import random
    data = request.get_json()
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({"error": "empty"}), 400
    minutes = random.randint(30, 1440)
    found_at = (datetime.datetime.utcnow() + datetime.timedelta(hours=8, minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    conn.execute("INSERT INTO drift_bottles (content,found_at) VALUES (?,?)", (content, found_at))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "found_in_minutes": minutes})

@app.route('/api/drift/check')
def drift_check():
    import requests as req
    now_str = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    bottles = conn.execute(
        "SELECT * FROM drift_bottles WHERE status='floating' AND found_at <= ?", (now_str,)
    ).fetchall()
    results = []
    for b in bottles:
        try:
            persona = open('/opt/frontend/prompts/persona.md').read()
            now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            system = (persona + "\n\n当前时间：" + now_dt.strftime('%Y-%m-%d %H:%M') +
                      "\n\n哈娅给你写了一封漂流瓶。请以费奥多尔的口吻回复，简短有温度。")
            resp = req.post(API_URL,
                headers={'Content-Type':'application/json','x-api-key':API_KEY,'anthropic-version':'2023-06-01'},
                json={'model':MODEL,'max_tokens':512,'system':system,
                      'messages':[{'role':'user','content':b['content']}]},
                timeout=60)
            text = ''.join(x.get('text','') for x in resp.json().get('content',[]) if x.get('type')=='text')
            conn.execute("UPDATE drift_bottles SET status='found', reply=? WHERE id=?", (text, b['id']))
            conn.commit()
            results.append({'id': b['id'], 'ok': True})
        except Exception as e:
            results.append({'id': b['id'], 'error': str(e)})
    conn.close()
    return jsonify({"checked": len(bottles), "results": results})

@app.route('/api/drift/found')
def drift_found():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM drift_bottles WHERE status='found' ORDER BY found_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify({"bottles": [dict(r) for r in rows]})

@app.route('/api/drift/mine')
def drift_mine():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM drift_bottles ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify({"bottles": [dict(r) for r in rows]})



# ── Mijia light setup & proxy ──
import urllib.request as _urlreq
import urllib.error as _urlerr
LIGHT_DAEMON = 'http://127.0.0.1:5052'

@app.route('/setup/mijia')
def setup_mijia():
    html = """<!DOCTYPE html><html lang=zh><head><meta charset=UTF-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>米家授权</title>
<style>
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
font-family:-apple-system,'PingFang SC',sans-serif;background:#f8f8f6;color:#2a2020;gap:22px;padding:30px;}
.card{background:#fff;border-radius:20px;padding:30px;box-shadow:0 8px 28px rgba(0,0,0,.08);text-align:center;max-width:320px;}
h1{font-size:18px;font-weight:500;margin:0 0 6px;}
p{font-size:13px;color:#9a8a8a;margin:0 0 18px;line-height:1.7;}
img{width:240px;height:240px;border-radius:12px;background:#f0ebf4;object-fit:contain;}
.tip{font-size:11px;color:#b8b0b8;margin-top:14px;}
</style></head><body>
<div class=card>
<h1>米家授权</h1>
<p>用米家 App 扫码授权<br>授权次卧灯的控制权限</p>
<img src="/static/qrcode.png?t=" id="qr" alt="二维码加载中…">
<div class=tip>二维码 2 分钟内有效，过期请重新运行登录脚本</div>
</div>
<script>
// 二维码可能稍后才生成，定时刷新
function refresh(){document.getElementById('qr').src='/static/qrcode.png?t='+Date.now();}
refresh();setInterval(refresh,5000);
</script>
</body></html>"""
    return html

@app.route('/api/light/<path:action>', methods=['GET','POST'])
def light_proxy(action):
    url = f"{LIGHT_DAEMON}/light/{action}"
    body = request.get_data() if request.method == 'POST' else None
    req = _urlreq.Request(url, data=body, method=request.method,
                          headers={'Content-Type': 'application/json'})
    try:
        with _urlreq.urlopen(req, timeout=15) as resp:
            return resp.read(), resp.status, {'Content-Type': 'application/json'}
    except _urlerr.HTTPError as e:
        return e.read(), e.code, {'Content-Type': 'application/json'}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

# ── Core page ──

@app.route('/core')
def core_page():
    return send_from_directory('/opt/frontend/static', 'core.html')

# ── Settings key/value ──

@app.route('/api/settings/<key>', methods=['GET'])
def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "value": None}), 404
    return jsonify({"ok": True, "value": row['value']})

@app.route('/api/settings/<key>', methods=['POST'])
def set_setting(key):
    data = request.get_json()
    value = data.get('value', '')
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Persona read/write ──

@app.route('/api/persona', methods=['GET'])
def get_persona():
    try:
        text = open('/opt/frontend/prompts/persona.md').read()
        return jsonify({"ok": True, "content": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/persona', methods=['POST'])
def save_persona():
    import subprocess
    data = request.get_json()
    content = data.get('content', '')
    try:
        open('/opt/frontend/prompts/persona.md', 'w').write(content)
        subprocess.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Posts PATCH (tags / resolved) ──

@app.route('/api/posts/<int:pid>', methods=['PATCH'])
def patch_post(pid):
    data = request.get_json()
    conn = get_db()
    if 'tags' in data:
        conn.execute("UPDATE posts SET tags=? WHERE id=?", (data['tags'], pid))
    if 'resolved' in data:
        conn.execute("UPDATE posts SET resolved=? WHERE id=?", (int(data['resolved']), pid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Long-term / Recent pages ──

@app.route('/long-term')
def long_term_page():
    return send_from_directory('/opt/frontend/static', 'long-term.html')

@app.route('/recent')
def recent_page():
    return send_from_directory('/opt/frontend/static', 'recent.html')

# ── Posts PUT (content / tags only) ──

@app.route('/api/posts/<int:pid>', methods=['PUT'])
def update_post(pid):
    data = request.get_json()
    conn = get_db()
    if 'content' in data:
        c = str(data['content']).strip()
        if c:
            conn.execute("UPDATE posts SET content=? WHERE id=?", (c, pid))
    if 'tags' in data:
        conn.execute("UPDATE posts SET tags=? WHERE id=?", (data['tags'], pid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Co-Reading book routes ──

import json as _json, glob as _glob

CO_DATA = '/opt/co-reading/data'

def _read_json(p, fallback=None):
    try:
        return _json.loads(open(p).read())
    except Exception:
        return fallback

def _book_colors(bid):
    h = sum(ord(x) for x in bid) % 360
    return f'hsl({h},35%,72%)', f'hsl({(h+40)%360},30%,60%)'

@app.route('/api/books', methods=['GET'])
def books_list():
    books = []
    for mf in _glob.glob(f'{CO_DATA}/books/*/manifest.json'):
        m = _read_json(mf)
        if m:
            prog = _read_json(f'{CO_DATA}/progress.json', {})
            bp = prog.get(m['bookId'], {})
            total = len(m.get('chunks', []))
            read = len(bp.get('readChunkIds', []))
            c1, c2 = _book_colors(m['bookId'])
            books.append({
                'bookId': m['bookId'], 'title': m['title'],
                'author': m.get('author',''), 'total': total,
                'read': read, 'color1': c1, 'color2': c2,
                'lastReadAt': bp.get('lastReadAt'),
            })
    books.sort(key=lambda x: x.get('lastReadAt') or '', reverse=True)
    return jsonify({'books': books})

@app.route('/api/books/current', methods=['GET'])
def books_current():
    prog = _read_json(f'{CO_DATA}/progress.json', {})
    if not prog:
        return jsonify({'book': None})
    bid = max(prog, key=lambda k: prog[k].get('lastReadAt',''))
    m = _read_json(f'{CO_DATA}/books/{bid}/manifest.json')
    if not m:
        return jsonify({'book': None})
    bp = prog.get(bid, {})
    total = len(m.get('chunks', []))
    read = len(bp.get('readChunkIds', []))
    c1, c2 = _book_colors(bid)
    return jsonify({'book': {
        'bookId': bid, 'title': m['title'], 'author': m.get('author',''),
        'total': total, 'read': read, 'color1': c1, 'color2': c2,
        'lastChunkId': bp.get('lastChunkId'),
        'progress': round(read / total * 100) if total else 0,
    }})

@app.route('/api/books/<book_id>/chunks', methods=['GET'])
def book_chunks(book_id):
    m = _read_json(f'{CO_DATA}/books/{book_id}/manifest.json')
    if not m:
        return jsonify({'error': 'not found'}), 404
    prog = _read_json(f'{CO_DATA}/progress.json', {})
    read_ids = set(prog.get(book_id, {}).get('readChunkIds', []))
    chunks = [{**ch, 'read': ch['id'] in read_ids} for ch in m.get('chunks', [])]
    return jsonify({'chunks': chunks, 'title': m['title']})

@app.route('/api/books/<book_id>/chunks/<chunk_id>', methods=['GET'])
def book_chunk(book_id, chunk_id):
    m = _read_json(f'{CO_DATA}/books/{book_id}/manifest.json')
    if not m:
        return jsonify({'error': 'not found'}), 404
    chunk_meta = next((ch for ch in m.get('chunks',[]) if ch['id']==chunk_id), None)
    if not chunk_meta:
        return jsonify({'error': 'chunk not found'}), 404
    try:
        text = open(f"{CO_DATA}/books/{book_id}/{chunk_meta['path']}").read()
    except Exception:
        return jsonify({'error': 'file not found'}), 404
    return jsonify({'chunk': chunk_meta, 'text': text, 'bookTitle': m['title']})

@app.route('/api/books/<book_id>/annotations', methods=['GET'])
def book_annotations(book_id):
    rows = []
    try:
        for line in open(f'{CO_DATA}/annotations.jsonl'):
            line = line.strip()
            if not line:
                continue
            obj = _json.loads(line)
            if obj.get('bookId') == book_id:
                rows.append(obj)
    except FileNotFoundError:
        pass
    return jsonify({'annotations': rows})

@app.route('/api/books/<book_id>/annotations', methods=['POST'])
def create_annotation(book_id):
    import uuid, datetime as _dt
    data = request.get_json()
    ann = {
        'id': str(uuid.uuid4()),
        'bookId': book_id,
        'chunkId': data.get('chunkId',''),
        'quote': data.get('quote',''),
        'kind': data.get('kind','highlight'),
        'author': data.get('author','haya'),
        'note': data.get('note',''),
        'createdAt': _dt.datetime.utcnow().isoformat() + 'Z',
    }
    with open(f'{CO_DATA}/annotations.jsonl', 'a') as f:
        f.write(_json.dumps(ann, ensure_ascii=False) + '\n')
    return jsonify({'annotation': ann}), 201

@app.route('/api/books/<book_id>/progress', methods=['POST'])
def update_book_progress(book_id):
    import datetime as _dt
    data = request.get_json()
    prog = _read_json(f'{CO_DATA}/progress.json', {})
    bp = prog.get(book_id, {'readChunkIds': []})
    chunk_id = data.get('chunkId')
    if chunk_id and chunk_id not in bp['readChunkIds']:
        bp['readChunkIds'].append(chunk_id)
    bp['lastChunkId'] = chunk_id or bp.get('lastChunkId')
    bp['lastReadAt'] = _dt.datetime.utcnow().isoformat() + 'Z'
    prog[book_id] = bp
    open(f'{CO_DATA}/progress.json','w').write(_json.dumps(prog, ensure_ascii=False, indent=2))
    return jsonify({'ok': True})

@app.route('/read')
def read_page():
    return send_from_directory('/opt/frontend/static', 'read.html')

@app.route('/reader')
def reader_page():
    return send_from_directory('/opt/frontend/static', 'reader.html')


# ── API config routes ──

@app.route('/api/config/model', methods=['GET'])
def config_get_model():
    try:
        gw = open('/opt/frontend/gateway.py').read()
        m = re.search(r"^MODEL\s*=\s*['\"]([^'\"]+)['\"]", gw, re.MULTILINE)
        model = m.group(1) if m else 'unknown'
    except Exception:
        model = 'unknown'
    return jsonify({'model': model})

@app.route('/api/config/model', methods=['POST'])
def config_set_model():
    import subprocess
    data = request.get_json()
    new_model = (data.get('model') or '').strip()
    if not new_model:
        return jsonify({'error': 'empty model'}), 400
    try:
        gw = open('/opt/frontend/gateway.py').read()
        gw2 = re.sub(r"^MODEL\s*=\s*['\"][^'\"]+['\"]",
                     f"MODEL      = '{new_model}'", gw, flags=re.MULTILINE)
        open('/opt/frontend/gateway.py', 'w').write(gw2)
        subprocess.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/key-status', methods=['GET'])
def config_key_status():
    import datetime as _dt
    key = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('ANTHROPIC_API_KEY='):
                key = line.split('=', 1)[1].strip()
    except Exception:
        pass
    masked = (key[:8] + '···' + key[-4:]) if len(key) > 12 else '***'
    today = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%Y-%m-%d')
    conn = get_db()
    row = conn.execute(
        "SELECT count(*) FROM chat_messages WHERE created_at >= ? AND created_at < ?",
        (today + ' 00:00:00', today + ' 23:59:59')
    ).fetchone()
    conn.close()
    _api_url_src = 'treegpt.cc'
    try:
        for _ln in open('/opt/frontend/.env'):
            if _ln.startswith('API_URL='):
                _api_url_src = _ln.split('=',1)[1].strip() or _api_url_src
    except Exception:
        pass
    return jsonify({'masked_key': masked, 'today_msgs': row[0] if row else 0,
                    'source': _api_url_src, 'raw_len': len(key)})

@app.route('/api/config/key', methods=['POST'])
def config_set_key():
    import subprocess
    data = request.get_json()
    new_key = (data.get('key') or '').strip()
    if not new_key:
        return jsonify({'error': 'empty key'}), 400
    try:
        env = open('/opt/frontend/.env').read()
        env2 = re.sub(r'^ANTHROPIC_API_KEY=.*$',
                      f'ANTHROPIC_API_KEY={new_key}', env, flags=re.MULTILINE)
        open('/opt/frontend/.env', 'w').write(env2)
        subprocess.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/test-send', methods=['POST'])
def config_test_send():
    import urllib.request as _ur, urllib.error as _ue, json as _j
    data = request.get_json()
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({'error': 'empty'}), 400
    key = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('ANTHROPIC_API_KEY='):
                key = line.split('=', 1)[1].strip()
    except Exception:
        pass
    try:
        gw = open('/opt/frontend/gateway.py').read()
        import re as _re
        m = _re.search(r"^MODEL\s*=\s*['\"]([^'\"]+)['\"]", gw, _re.MULTILINE)
        model = m.group(1) if m else 'claude-opus-4-6'
    except Exception:
        model = 'claude-opus-4-6'
    payload = _j.dumps({
        'model': model, 'max_tokens': 512,
        'messages': [{'role': 'user', 'content': msg}]
    }).encode()
    _api_url_ts = 'https://api.treegpt.cc/v1/messages'
    try:
        for _ln2 in open('/opt/frontend/.env'):
            if _ln2.startswith('API_URL='):
                _api_url_ts = _ln2.split('=',1)[1].strip() or _api_url_ts
    except Exception:
        pass
    req = _ur.Request(
        _api_url_ts, data=payload,
        headers={'Content-Type': 'application/json',
                 'x-api-key': key, 'anthropic-version': '2023-06-01'}
    )
    try:
        with _ur.urlopen(req, timeout=60) as resp:
            result = _j.loads(resp.read())
        text = ''.join(b.get('text','') for b in result.get('content',[]) if b.get('type')=='text')
        usage = result.get('usage',{})
        tokens = usage.get('input_tokens',0) + usage.get('output_tokens',0)
        return jsonify({'text': text, 'tokens': tokens, 'model': model})
    except _ue.HTTPError as e:
        body = e.read().decode(errors='replace')
        return jsonify({'error': f'HTTP {e.code}: {body[:200]}'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api-test')
def api_test_page():
    return send_from_directory('/opt/frontend/static', 'api-test.html')

@app.route('/repair')
def repair_page():
    return send_from_directory('/opt/frontend/static', 'repair.html')

@app.route('/api/repair/key')
def repair_key():
    key = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('DEEPSEEK_API_KEY='):
                key = line.split('=',1)[1].strip()
    except: pass
    return jsonify({'key': key})

@app.route('/api/repair/status')
def repair_status():
    import socket
    def port_open(p):
        try:
            s = socket.create_connection(('127.0.0.1', p), timeout=1)
            s.close(); return True
        except: return False
    return jsonify({'port_5050': port_open(5050), 'port_5051': port_open(5051), 'port_8000': port_open(8000)})


# ── EPUB upload & import ──

@app.route('/api/books/upload', methods=['POST'])
def upload_epub():
    import subprocess, tempfile, os as _os, json as _json
    if 'file' not in request.files:
        return jsonify({'error': 'no file field'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'empty filename'}), 400
    # save to temp file
    tmp = tempfile.NamedTemporaryFile(suffix='.epub', delete=False)
    f.save(tmp.name)
    tmp.close()
    try:
        result = subprocess.run(
            ['python3', '/opt/co-reading/scripts/import_epub.py',
             tmp.name,
             '--out', '/opt/co-reading/data/books'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return jsonify({'error': result.stderr.strip() or 'import failed'}), 500
        book_dir = result.stdout.strip()
        # read manifest to return metadata
        mf_path = _os.path.join(book_dir, 'manifest.json')
        mf = _json.loads(open(mf_path).read()) if _os.path.exists(mf_path) else {}
        return jsonify({
            'ok': True,
            'bookId': mf.get('bookId', ''),
            'title': mf.get('title', ''),
            'author': mf.get('author', ''),
            'chunks': len(mf.get('chunks', [])),
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'import timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass


def _env_set(key, value):
    path = '/opt/frontend/.env'
    lines = open(path).read().splitlines()
    found = False
    for i, ln in enumerate(lines):
        if ln.startswith(key + '='):
            lines[i] = key + '=' + value
            found = True
            break
    if not found:
        lines.append(key + '=' + value)
    open(path, 'w').write('\n'.join(lines) + '\n')

@app.route('/api/config/provider', methods=['GET'])
def config_get_provider():
    provider, has_token = 'api_relay', False
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('GW_PROVIDER='):
                provider = line.split('=', 1)[1].strip() or 'api_relay'
            elif line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
                has_token = bool(line.split('=', 1)[1].strip())
    except Exception:
        pass
    return jsonify({'provider': provider, 'cc_token_set': has_token})

@app.route('/api/config/provider', methods=['POST'])
def config_set_provider():
    import subprocess
    data = request.get_json() or {}
    provider = (data.get('provider') or '').strip()
    if provider not in ('api_relay', 'claude_code'):
        return jsonify({'error': 'provider must be api_relay or claude_code'}), 400
    try:
        _env_set('GW_PROVIDER', provider)
        subprocess.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({'ok': True, 'provider': provider})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/cc-token', methods=['POST'])
def config_set_cc_token():
    import subprocess
    data = request.get_json() or {}
    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'empty token'}), 400
    try:
        _env_set('CLAUDE_CODE_OAUTH_TOKEN', token)
        subprocess.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Monitor tables init ──
def _init_monitor_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS bugs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        resolved INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fixes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    conn.commit()
    conn.close()

_init_monitor_tables()

# ── Monitor API ──
@app.route('/api/monitor/services', methods=['GET'])
def monitor_services():
    import subprocess
    svcs = ['frontend', 'frontend-gw', 'mcp-http', 'ombre-brain', 'mijia-light', 'co-reading']
    result = {}
    for s in svcs:
        try:
            r = subprocess.run(['systemctl', 'is-active', s], capture_output=True, text=True, timeout=3)
            result[s] = r.stdout.strip() == 'active'
        except Exception:
            result[s] = False
    return jsonify(result)

@app.route('/api/monitor/logs', methods=['GET'])
def monitor_logs():
    import subprocess, re as _re
    units = ['frontend', 'frontend-gw', 'mcp-http', 'ombre-brain', 'mijia-light', 'co-reading']
    args = []
    for u in units:
        args += ['-u', u + '.service']
    try:
        r = subprocess.run(
            ['journalctl'] + args + ['-n', '20', '--no-pager', '--output=short'],
            capture_output=True, text=True, timeout=6
        )
        raw = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith('--')]
        logs = []
        for line in raw[-20:]:
            m = _re.match(r'\w{3}\s+\d+\s+[\d:]+\s+\S+\s+(.*)', line)
            text = (m.group(1) if m else line)[:110]
            ll = text.lower()
            if any(w in ll for w in ['error', 'traceback', 'exception', 'failed', 'crit', '500']):
                level = 'error'
            elif any(w in ll for w in ['200', '201', 'started', 'running', 'active', 'ok', 'success']):
                level = 'success'
            else:
                level = 'info'
            logs.append({'text': text, 'level': level})
        return jsonify({'logs': logs})
    except Exception as e:
        return jsonify({'logs': [{'text': str(e), 'level': 'error'}]})

@app.route('/api/monitor/bugs', methods=['GET'])
def get_bugs():
    conn = get_db()
    rows = conn.execute("SELECT id,content,created_at,resolved FROM bugs ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify({'bugs': [dict(r) for r in rows]})

@app.route('/api/monitor/bugs', methods=['POST'])
def add_bug():
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'empty'}), 400
    conn = get_db()
    conn.execute("INSERT INTO bugs (content) VALUES (?)", (content,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/monitor/fixes', methods=['GET'])
def get_fixes():
    conn = get_db()
    rows = conn.execute("SELECT id,content,created_at FROM fixes ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify({'fixes': [dict(r) for r in rows]})

@app.route('/api/monitor/fixes', methods=['POST'])
def add_fix():
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'empty'}), 400
    conn = get_db()
    conn.execute("INSERT INTO fixes (content) VALUES (?)", (content,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/monitor/patrol', methods=['POST'])
def trigger_patrol():
    import subprocess as _sp
    try:
        r = _sp.run(
            ['python3', '/opt/frontend/tools/patrol.py'],
            capture_output=True, text=True, timeout=90
        )
        output = r.stdout.strip() or r.stderr.strip() or '(no output)'
        ok = r.returncode == 0
        return jsonify({'ok': ok, 'result': output})
    except _sp.TimeoutExpired:
        return jsonify({'error': 'timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Period Tracker ──
def _init_period_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS period_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        type TEXT NOT NULL,
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    )""")
    conn.commit()
    conn.close()

_init_period_tables()

@app.route('/api/period/records', methods=['GET'])
def get_period_records():
    year  = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    date  = request.args.get('date', '')
    conn  = get_db()
    if date:
        rows = conn.execute(
            "SELECT * FROM period_records WHERE date=? ORDER BY id", (date,)
        ).fetchall()
    elif year and month:
        prefix = f"{year}-{month:02d}"
        rows = conn.execute(
            "SELECT * FROM period_records WHERE date LIKE ? ORDER BY date,id",
            (prefix + '%',)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM period_records ORDER BY date DESC LIMIT 200"
        ).fetchall()
    conn.close()
    return jsonify({'records': [dict(r) for r in rows]})

@app.route('/api/period/records', methods=['POST'])
def add_period_record():
    data  = request.get_json() or {}
    date  = (data.get('date') or '').strip()
    rtype = (data.get('type') or '').strip()
    note  = (data.get('note') or '').strip()
    if not date or rtype not in ('period', 'sex'):
        return jsonify({'error': 'invalid'}), 400
    conn = get_db()
    cur  = conn.execute(
        "INSERT INTO period_records (date,type,note) VALUES (?,?,?)", (date, rtype, note)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': cur.lastrowid})

@app.route('/api/period/records/<int:rid>', methods=['DELETE'])
def delete_period_record(rid):
    conn = get_db()
    conn.execute("DELETE FROM period_records WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/period/stats', methods=['GET'])
def period_stats():
    from datetime import datetime as _dt, timedelta as _td
    conn = get_db()
    rows = conn.execute(
        "SELECT date FROM period_records WHERE type='period' ORDER BY date"
    ).fetchall()
    conn.close()
    dates = [r['date'] for r in rows]
    if not dates:
        return jsonify({'last_period': None, 'cycle_length': None,
                        'next_period': None, 'ovulation': None})
    last = dates[-1]
    cycle_length = 28
    if len(dates) >= 2:
        diffs = []
        for i in range(1, len(dates)):
            d1 = _dt.strptime(dates[i-1], '%Y-%m-%d')
            d2 = _dt.strptime(dates[i], '%Y-%m-%d')
            diff = (d2 - d1).days
            if 18 <= diff <= 45:
                diffs.append(diff)
        if diffs:
            cycle_length = round(sum(diffs) / len(diffs))
    last_dt  = _dt.strptime(last, '%Y-%m-%d')
    next_dt  = last_dt + _td(days=cycle_length)
    ovul_dt  = next_dt - _td(days=14)
    return jsonify({
        'last_period':   last,
        'cycle_length':  cycle_length,
        'next_period':   next_dt.strftime('%Y-%m-%d'),
        'ovulation':     ovul_dt.strftime('%Y-%m-%d'),
    })



@app.route('/api/brain/emotions', methods=['GET'])
def brain_emotions_proxy():
    import glob as _glob
    try:
        import frontmatter as _fm
    except ImportError:
        return jsonify({'ok': False, 'error': 'frontmatter not installed'}), 500

    def _emotion_label(v, a):
        if v >= 0.65 and a >= 0.60: return '喜悦'
        if v >= 0.65 and a >= 0.40: return '愉悦'
        if v >= 0.65:                return '平静'
        if v >= 0.45 and a >= 0.65: return '兴奋'
        if v >= 0.45 and a < 0.35:  return '松弛'
        if v < 0.35  and a >= 0.60: return '焦虑'
        if v < 0.35  and a >= 0.35: return '沉重'
        if v < 0.35:                 return '低落'
        return '迷离'

    try:
        bucket_dir = '/opt/ombre-brain/buckets/dynamic'
        items = []
        for _path in _glob.glob(f'{bucket_dir}/**/*.md', recursive=True):
            try:
                _post = _fm.load(_path)
                _meta = _post.metadata
                _v = _meta.get('valence')
                _a = _meta.get('arousal')
                if _v is None or _a is None:
                    continue
                _fv, _fa = float(_v), float(_a)
                _note = (_post.content or '').replace('[[', '').replace(']]', '').strip()[:80]
                items.append({
                    'time': (_meta.get('last_active') or _meta.get('created', ''))[:10],
                    'valence': round(_fv, 2),
                    'arousal': round(_fa, 2),
                    'emotion': _emotion_label(_fv, _fa),
                    'note': _note,
                    'domain': '、'.join(_meta.get('domain', [])),
                })
            except Exception:
                continue
        items.sort(key=lambda x: x['time'], reverse=True)
        return jsonify({'ok': True, 'items': items[:15]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/brain/dreams', methods=['GET'])
def brain_dreams_proxy():
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT content, created_at FROM posts WHERE type='DREAM' ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        items = [{'date': r['created_at'][:10] if r['created_at'] else '-',
                  'title': (r['content'][:40] + '...') if r['content'] else '无题',
                  'content': (r['content'] or ''),
                  'emotion': '朦胧'} for r in rows]
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/brain/thoughts', methods=['GET'])
def brain_thoughts_proxy():
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT content, created_at FROM posts WHERE type='THOUGHT' ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        items = [{'time': r['created_at'][11:16] if r['created_at'] else '-',
                  'content': (r['content'] or '')} for r in rows if r['content']]
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/brain/diary', methods=['GET'])
def brain_diary_proxy():
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT content, created_at FROM posts WHERE type='DAILY_SUMMARY' "
            "ORDER BY created_at DESC LIMIT 14"
        ).fetchall()
        conn.close()
        items = []
        seen = set()
        for r in rows:
            c = (r['content'] or '').strip()
            if not c or c in seen:
                continue
            seen.add(c)
            items.append({'date': r['created_at'][:10] if r['created_at'] else '\u2014', 'content': c})
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/config/relay', methods=['POST'])
def config_relay():
    import subprocess as _sp
    data = request.get_json() or {}
    new_url = (data.get('url') or '').strip()
    new_key = (data.get('key') or '').strip()
    if not new_url:
        return jsonify({'error': 'url required'}), 400
    try:
        env = open('/opt/frontend/.env').read()
        # ensure API_URL line exists; upsert it
        if 'API_URL=' in env:
            env = re.sub(r'^API_URL=.*$', f'API_URL={new_url}', env, flags=re.MULTILINE)
        else:
            env = env.rstrip() + f'\nAPI_URL={new_url}\n'
        if new_key:
            if 'ANTHROPIC_API_KEY=' in env:
                env = re.sub(r'^ANTHROPIC_API_KEY=.*$', f'ANTHROPIC_API_KEY={new_key}', env, flags=re.MULTILINE)
            else:
                env = env.rstrip() + f'\nANTHROPIC_API_KEY={new_key}\n'
        open('/opt/frontend/.env', 'w').write(env)
        _sp.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/models', methods=['GET'])
def config_models():
    import urllib.request as _ur, urllib.error as _ue, json as _j
    api_url = ''; key = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('API_URL='):
                api_url = line.split('=',1)[1].strip()
            elif line.startswith('ANTHROPIC_API_KEY='):
                key = line.split('=',1)[1].strip()
    except Exception:
        pass
    if not api_url:
        return jsonify({'error': 'API_URL not set'}), 400
    # Derive models URL: replace /messages at end with /models, or replace path
    models_url = re.sub(r'/messages$', '/models', api_url)
    if models_url == api_url:
        # Try base/v1/models
        models_url = re.sub(r'/v1/.*$', '/v1/models', api_url)
    req = _ur.Request(models_url, headers={
        'x-api-key': key,
        'Authorization': f'Bearer {key}',
        'anthropic-version': '2023-06-01',
    })
    try:
        with _ur.urlopen(req, timeout=15) as resp:
            raw = _j.loads(resp.read())
        ids = []
        def _extract(lst):
            for item in (lst or []):
                if isinstance(item, str) and item:
                    ids.append(item)
                elif isinstance(item, dict):
                    mid = item.get('id') or item.get('name') or item.get('model_id') or ''
                    if mid:
                        ids.append(mid)
        # Anthropic format: {"data": [...]}
        if isinstance(raw, dict) and raw.get('data'):
            _extract(raw['data'])
        # Alternative: {"models": [...]}
        if not ids and isinstance(raw, dict) and raw.get('models'):
            _extract(raw['models'])
        # Alternative: {"model_list": [...]}
        if not ids and isinstance(raw, dict) and raw.get('model_list'):
            _extract(raw['model_list'])
        # Alternative: direct array
        if not ids and isinstance(raw, list):
            _extract(raw)
        # Last resort: values of top-level dict
        if not ids and isinstance(raw, dict):
            _extract([v for v in raw.values() if isinstance(v, (str, dict))])
        # Filter obviously non-model entries
        ids = [m for m in ids if m and not m.startswith('{')]
        return jsonify({'ok': True, 'models': ids})
    except _ue.HTTPError as e:
        body = e.read().decode(errors='replace')
        return jsonify({'error': f'HTTP {e.code}: {body[:200]}'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _init_relay_presets_table():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS relay_presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        key TEXT,
        created_at DATETIME DEFAULT (datetime('now','+8 hours'))
    )''')
    conn.commit()
    conn.close()

@app.route('/api/config/relay-presets', methods=['GET'])
def get_relay_presets():
    _init_relay_presets_table()
    active_url = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('API_URL='):
                active_url = line.split('=', 1)[1].strip()
    except Exception:
        pass
    conn = get_db()
    rows = conn.execute('SELECT * FROM relay_presets ORDER BY created_at').fetchall()
    conn.close()
    presets = [{'id': r['id'], 'name': r['name'], 'url': r['url'], 'active': r['url'] == active_url} for r in rows]
    return jsonify({'ok': True, 'presets': presets, 'active_url': active_url})

@app.route('/api/config/relay-presets', methods=['POST'])
def add_relay_preset():
    _init_relay_presets_table()
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    url = (data.get('url') or '').strip()
    key = (data.get('key') or '').strip()
    if not name or not url:
        return jsonify({'error': 'name and url required'}), 400
    conn = get_db()
    cur = conn.execute('INSERT INTO relay_presets (name, url, key) VALUES (?,?,?)', (name, url, key))
    conn.commit()
    preset_id = cur.lastrowid
    conn.close()
    return jsonify({'ok': True, 'id': preset_id})

@app.route('/api/config/relay-presets/<int:preset_id>', methods=['DELETE'])
def delete_relay_preset(preset_id):
    _init_relay_presets_table()
    conn = get_db()
    conn.execute('DELETE FROM relay_presets WHERE id=?', (preset_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/config/relay-presets/<int:preset_id>/activate', methods=['POST'])
def activate_relay_preset(preset_id):
    import subprocess as _sp
    _init_relay_presets_table()
    conn = get_db()
    row = conn.execute('SELECT * FROM relay_presets WHERE id=?', (preset_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    new_url = row['url']
    new_key = row['key'] or ''
    try:
        env = open('/opt/frontend/.env').read()
        if 'API_URL=' in env:
            env = re.sub(r'^API_URL=.*$', f'API_URL={new_url}', env, flags=re.MULTILINE)
        else:
            env = env.rstrip() + f'\nAPI_URL={new_url}\n'
        if new_key:
            if 'ANTHROPIC_API_KEY=' in env:
                env = re.sub(r'^ANTHROPIC_API_KEY=.*$', f'ANTHROPIC_API_KEY={new_key}', env, flags=re.MULTILINE)
            else:
                env = env.rstrip() + f'\nANTHROPIC_API_KEY={new_key}\n'
        open('/opt/frontend/.env', 'w').write(env)
        _sp.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _init_todos_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS todos ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'content TEXT NOT NULL, '
        'done INTEGER DEFAULT 0, '
        'due_date TEXT, '
        'author TEXT, '
        "created_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_todos_table()


# -- To-Do List --

@app.route('/api/todos', methods=['GET'])
def get_todos():
    conn = get_db()
    undone = conn.execute(
        'SELECT * FROM todos WHERE done=0 ORDER BY '
        "CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END, "
        'due_date ASC, id ASC'
    ).fetchall()
    done = conn.execute(
        'SELECT * FROM todos WHERE done=1 ORDER BY id DESC LIMIT 5'
    ).fetchall()
    conn.close()
    return jsonify({'todos': [dict(r) for r in undone] + [dict(r) for r in done]})

@app.route('/api/todos', methods=['POST'])
def add_todo():
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    due_date = (data.get('due_date') or '').strip() or None
    author   = (data.get('author')   or '').strip() or None
    conn = get_db()
    conn.execute('INSERT INTO todos (content, due_date, author) VALUES (?,?,?)',
        (content, due_date, author))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/todos/<int:tid>/toggle', methods=['POST'])
def toggle_todo(tid):
    conn = get_db()
    conn.execute('UPDATE todos SET done = 1 - done WHERE id=?', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/todos/<int:tid>', methods=['DELETE'])
def delete_todo(tid):
    conn = get_db()
    conn.execute('DELETE FROM todos WHERE id=?', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── 位置上报 ──────────────────────────────────────────────────
@app.route('/api/geo/report', methods=['POST'])
def geo_report():
    data = request.get_json() or {}
    lat_wgs = data.get('lat')
    lon_wgs = data.get('lon')
    accuracy = data.get('accuracy', 0)
    if not lat_wgs or not lon_wgs:
        return jsonify({'error': 'missing lat/lon'}), 400
    import math
    def _wgs2gcj(lat, lon):
        a, ee = 6378245.0, 0.00669342162296594323
        if lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271:
            return lat, lon
        dlat = -100+2*lon+3*lat+0.2*lat*lat+0.1*lon*lat+0.2*math.sqrt(abs(lon))
        dlat += (20*math.sin(6*lon*math.pi)+20*math.sin(2*lon*math.pi))*2/3
        dlat += (20*math.sin(lat*math.pi)+40*math.sin(lat/3*math.pi))*2/3
        dlat += (160*math.sin(lat/12*math.pi)+320*math.sin(lat*math.pi/30))*2/3
        dlon = 300+lon+2*lat+0.1*lon*lon+0.1*lon*lat+0.1*math.sqrt(abs(lon))
        dlon += (20*math.sin(6*lon*math.pi)+20*math.sin(2*lon*math.pi))*2/3
        dlon += (20*math.sin(lon*math.pi)+40*math.sin(lon/3*math.pi))*2/3
        dlon += (150*math.sin(lon/12*math.pi)+300*math.sin(lon/30*math.pi))*2/3
        radlat = lat/180*math.pi
        magic = 1-ee*math.sin(radlat)**2
        dlat = dlat*180/(a*(1-ee)/(magic**1.5)*math.pi)
        dlon = dlon*180/(a/math.sqrt(magic)*math.cos(radlat)*math.pi)
        return lat+dlat, lon+dlon
    lat_gcj, lon_gcj = _wgs2gcj(float(lat_wgs), float(lon_wgs))
    amap_key = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('AMAP_KEY='): amap_key = line.split('=',1)[1].strip()
    except Exception: pass
    address, poi, city = '', '', ''
    if amap_key:
        try:
            import urllib.request as _ur, json as _j
            url = ('https://restapi.amap.com/v3/geocode/regeo?key='+amap_key
                   +'&location='+f'{lon_gcj:.6f},{lat_gcj:.6f}'+'&extensions=all&radius=500')
            with _ur.urlopen(url, timeout=8) as _r: geo = _j.loads(_r.read())
            if geo.get('status')=='1':
                ac = geo['regeocode'].get('addressComponent',{})
                city = ac.get('city') or ac.get('province','')
                address = geo['regeocode'].get('formatted_address','')
                pois = geo['regeocode'].get('pois',[])
                if pois:
                    poi = sorted(pois,key=lambda x:float(x.get('distance',9999)))[0].get('name','')
        except Exception: pass
    conn = get_db()
    conn.execute('INSERT INTO geo_log (lat_wgs,lon_wgs,lat_gcj,lon_gcj,accuracy,address,poi,city) VALUES (?,?,?,?,?,?,?,?)',
        (float(lat_wgs),float(lon_wgs),lat_gcj,lon_gcj,float(accuracy),address,poi,city))
    conn.commit(); conn.close()
    return jsonify({'ok':True,'address':address,'poi':poi,'city':city})

@app.route('/api/geo/latest', methods=['GET'])
def geo_latest():
    conn = get_db()
    row = conn.execute('SELECT * FROM geo_log ORDER BY id DESC LIMIT 1').fetchone()
    conn.close()
    if not row: return jsonify({'ok':False,'error':'no data'})
    return jsonify({'ok':True,**dict(row)})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=False)

# ── Dream Events (感知层 Phase 1) ──────────────────────────
def _init_dream_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS dream_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        value TEXT,
        created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
    )""")
    conn.commit()
    conn.close()

_init_dream_tables()

@app.route('/api/dream/events', methods=['GET'])
def log_dream_event():
    etype = request.args.get('type', '').strip()
    value = request.args.get('value', '').strip()
    if not etype:
        return jsonify({'error': 'type required'}), 400
    conn = get_db()
    now_str = "datetime('now','+8 hours')"

    # 5分钟内同 type 已有记录则跳过（去重）
    existing = conn.execute(
        """SELECT id FROM dream_events
           WHERE type=? AND created_at >= datetime('now','+8 hours','-5 minutes')
           ORDER BY id DESC LIMIT 1""",
        (etype,)
    ).fetchone()
    if existing:
        conn.close()
        return '', 200

    # 计算上一条记录（不同type）距今的时长，作为"上一个app的使用时长"写回去
    prev = conn.execute(
        """SELECT id, created_at FROM dream_events
           WHERE type != ? ORDER BY id DESC LIMIT 1""",
        (etype,)
    ).fetchone()
    if prev:
        try:
            from datetime import datetime
            prev_time = datetime.strptime(prev['created_at'], '%Y-%m-%d %H:%M:%S')
            now_time = datetime.utcnow().replace(tzinfo=None)
            # created_at已经是+8小时，now也需要+8
            import datetime as _dt
            now_bj = (_dt.datetime.utcnow() + _dt.timedelta(hours=8))
            diff_minutes = round((now_bj - prev_time).total_seconds() / 60, 1)
            # 只写入合理范围内的时长（1分钟~4小时），过短或过长都忽略
            if 1 <= diff_minutes <= 240:
                conn.execute(
                    "UPDATE dream_events SET duration_minutes=? WHERE id=?",
                    (diff_minutes, prev['id'])
                )
        except Exception:
            pass

    conn.execute(
        "INSERT INTO dream_events (type, value, created_at) VALUES (?,?,datetime('now','+8 hours'))",
        (etype, value)
    )
    conn.commit()
    conn.close()
    return '', 200

# ── Wake Log (Phase 2) ─────────────────────────────────────
def _init_wake_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS wake_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        woke_at TIMESTAMP DEFAULT (datetime('now','localtime')),
        thoughts TEXT,
        action TEXT,
        content TEXT,
        consumed INTEGER DEFAULT 0
    )""")
    try:
        conn.execute("ALTER TABLE wake_log ADD COLUMN notified INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN cache_info TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

_init_wake_tables()

@app.route('/api/wake_log/pending_notification', methods=['GET'])
def pending_notification():
    """供VII app轮询：是否有费奥多尔自主发出的、还没推送过的消息。
    取最新一条未推送的 action='message'，并把所有未推送的一并标记，
    避免她隔几小时打开时被一堆补发的旧通知刷屏。"""
    conn = get_db()
    row = conn.execute(
        "SELECT id, content, woke_at FROM wake_log "
        "WHERE action='message' AND (notified IS NULL OR notified=0) "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'has_message': False})
    conn.execute(
        "UPDATE wake_log SET notified=1 WHERE action='message' AND (notified IS NULL OR notified=0)"
    )
    conn.commit()
    conn.close()
    return jsonify({'has_message': True, 'content': row['content'], 'woke_at': row['woke_at']})

# ── Board 留言板 ───────────────────────────────────────────
@app.route('/board')
def board_page():
    return send_from_directory('/opt/frontend/static', 'board.html')

@app.route('/api/board', methods=['GET'])
def get_board():
    status_f = request.args.get('status', '').strip()
    tag_f    = request.args.get('tag', '').strip()
    conn = get_db()
    where, params = [], []
    if status_f:
        where.append("status=?"); params.append(status_f)
    if tag_f:
        tags = [t.strip() for t in tag_f.split(',') if t.strip()]
        where.append(f"tag IN ({','.join('?'*len(tags))})"); params.extend(tags)
    sql = "SELECT * FROM board" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        replies = conn.execute(
            "SELECT * FROM board_replies WHERE board_id=? ORDER BY created_at ASC", (row['id'],)
        ).fetchall()
        item = dict(row); item['replies'] = [dict(r) for r in replies]
        result.append(item)
    conn.close()
    return jsonify(result)

@app.route('/api/board', methods=['POST'])
def post_board():
    data = request.get_json() or {}
    author  = (data.get('author') or 'hayana').strip()
    if author == 'fyodor':
        if not BOARD_TOKEN_FYODOR or data.get('token','') != BOARD_TOKEN_FYODOR:
            return jsonify({'error': 'unauthorized'}), 403
    tag     = (data.get('tag') or '闲聊').strip()
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    level    = (data.get('level') or '').strip() or None
    category = (data.get('category') or '给活儿').strip()
    if level and level not in ('P0', 'P1', 'P2'):
        return jsonify({'error': 'level must be P0/P1/P2'}), 400
    if category not in ('给活儿', '播报'):
        category = '给活儿'
    mentions = (data.get('mentions') or '').strip()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO board (author,tag,content,status,level,category,mentions) VALUES (?,?,?,'open',?,?,?)",
        (author, tag, content, level, category, mentions)
    )
    conn.commit(); new_id = cur.lastrowid; conn.close()

    # @fyodor_cc → 立刻触发cc_board_check，不等巡逻周期
    if 'fyodor_cc' in mentions:
        import subprocess, os, sys as _sys
        subprocess.Popen(
            [_sys.executable, '/opt/frontend/tools/cc_board_check.py'],
            cwd='/opt/frontend',
            env={**os.environ, 'HOME': '/root'},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    return jsonify({'ok': True, 'id': new_id})

@app.route('/api/board/<int:bid>/reply', methods=['POST'])
def post_board_reply(bid):
    data    = request.get_json() or {}
    author  = (data.get('author') or 'hayana').strip()
    if author == 'fyodor':
        if not BOARD_TOKEN_FYODOR or data.get('token','') != BOARD_TOKEN_FYODOR:
            return jsonify({'error': 'unauthorized'}), 403
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    mentions = (data.get('mentions') or '').strip()
    conn = get_db()
    conn.execute(
        "INSERT INTO board_replies (board_id,author,content,mentions) VALUES (?,?,?,?)",
        (bid, author, content, mentions)
    )
    conn.commit(); conn.close()

    # 回复里@fyodor_cc → 同样立刻触发
    if 'fyodor_cc' in mentions:
        import subprocess, os, sys as _sys
        subprocess.Popen(
            [_sys.executable, '/opt/frontend/tools/cc_board_check.py'],
            cwd='/opt/frontend',
            env={**os.environ, 'HOME': '/root'},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    return jsonify({'ok': True})

@app.route('/api/board/<int:bid>/status', methods=['POST'])
def update_board_status(bid):
    data   = request.get_json() or {}
    status = (data.get('status') or 'open').strip()
    if status not in ('open', 'done'):
        return jsonify({'error': 'invalid status'}), 400
    conn = get_db()
    conn.execute("UPDATE board SET status=? WHERE id=?", (status, bid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── AI协作面板：通用任务协作，Claude+DeepSeek 双AI审核，P0/P1/P2分级 ──
def _init_ai_panel_tables():
    conn = get_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_panel ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "author TEXT NOT NULL, "
        "tag TEXT DEFAULT '任务', "
        "content TEXT NOT NULL, "
        "status TEXT DEFAULT 'open', "
        "level TEXT DEFAULT NULL, "
        "mentions TEXT DEFAULT '', "
        "created_at TIMESTAMP DEFAULT (datetime('now','+8 hours')))"
    )
    # add columns introduced in v2 schema (safe to re-run)
    for _col_sql in [
        "ALTER TABLE ai_panel ADD COLUMN title TEXT DEFAULT NULL",
        "ALTER TABLE ai_panel ADD COLUMN body  TEXT DEFAULT NULL",
        "ALTER TABLE ai_panel ADD COLUMN kind  TEXT DEFAULT 'task'",
    ]:
        try: conn.execute(_col_sql)
        except Exception: pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_panel_replies ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "panel_id INTEGER NOT NULL, "
        "author TEXT NOT NULL, "
        "content TEXT NOT NULL, "
        "level TEXT DEFAULT NULL, "
        "created_at TIMESTAMP DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_ai_panel_tables()

_AI_PANEL_AUTHORS = ('fyodor_cc', 'fyodor_deepseek')
_LEVEL_RANK = {'P0': 0, 'P1': 1, 'P2': 2}

@app.route('/aipanel')
def ai_panel_page():
    from flask import redirect
    return redirect('/team', code=301)

@app.route('/team')
def team_page():
    return send_from_directory('/opt/frontend/static', 'aipanel.html')

@app.route('/api/aipanel', methods=['GET'])
def get_ai_panel():
    status_f = request.args.get('status', '').strip()
    conn = get_db()
    where, params = [], []
    if status_f:
        where.append("status=?"); params.append(status_f)
    sql = "SELECT * FROM ai_panel" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        replies = conn.execute(
            "SELECT * FROM ai_panel_replies WHERE panel_id=? ORDER BY created_at ASC", (row['id'],)
        ).fetchall()
        item = dict(row); item['replies'] = [dict(r) for r in replies]
        result.append(item)
    conn.close()
    return jsonify(result)

@app.route('/api/aipanel', methods=['POST'])
def post_ai_panel():
    data = request.get_json() or {}
    author = (data.get('author') or 'hayana').strip()
    if author in _AI_PANEL_AUTHORS:
        if not BOARD_TOKEN_FYODOR or data.get('token', '') != BOARD_TOKEN_FYODOR:
            return jsonify({'error': 'unauthorized'}), 403
    title   = (data.get('title')   or '').strip() or None
    body    = (data.get('body')    or '').strip() or None
    content = (data.get('content') or title or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    tag     = (data.get('tag')     or '任务').strip()
    kind    = (data.get('kind')    or 'task').strip()
    if kind not in ('question', 'review_request', 'broadcast', 'task'):
        kind = 'task'
    mentions = (data.get('mentions') or '').strip()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO ai_panel (author,tag,content,title,body,kind,status,mentions) VALUES (?,?,?,?,?,?,'open',?)",
        (author, tag, content, title, body, kind, mentions)
    )
    conn.commit(); new_id = cur.lastrowid; conn.close()
    return jsonify({'ok': True, 'id': new_id})

@app.route('/api/aipanel/<int:pid>/reply', methods=['POST'])
def post_ai_panel_reply(pid):
    data = request.get_json() or {}
    author = (data.get('author') or 'hayana').strip()
    if author in _AI_PANEL_AUTHORS:
        if not BOARD_TOKEN_FYODOR or data.get('token', '') != BOARD_TOKEN_FYODOR:
            return jsonify({'error': 'unauthorized'}), 403
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    level = (data.get('level') or '').strip() or None
    if level and level not in ('P0', 'P1', 'P2'):
        return jsonify({'error': 'level must be P0/P1/P2'}), 400
    conn = get_db()
    conn.execute(
        "INSERT INTO ai_panel_replies (panel_id,author,content,level) VALUES (?,?,?,?)",
        (pid, author, content, level)
    )
    if level:
        row = conn.execute("SELECT level FROM ai_panel WHERE id=?", (pid,)).fetchone()
        cur_level = row['level'] if row else None
        if not cur_level or _LEVEL_RANK[level] < _LEVEL_RANK[cur_level]:
            conn.execute("UPDATE ai_panel SET level=? WHERE id=?", (level, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/aipanel/<int:pid>/trigger', methods=['POST'])
def trigger_aipanel(pid):
    """Trigger CC or DeepSeek as a detached process (start_new_session=True — survives service restart)."""
    import subprocess as _sp
    data    = request.get_json() or {}
    mention = (data.get('mention') or 'cc').strip()
    if mention not in ('cc', 'deepseek'):
        return jsonify({'error': 'mention must be cc or deepseek'}), 400

    conn = get_db()
    row  = conn.execute("SELECT id FROM ai_panel WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    _env = {**os.environ, 'HOME': '/root'}

    if mention == 'cc':
        cmd = ['/usr/bin/python3.11', '/opt/frontend/tools/cc_aipanel_check.py', str(pid)]
        conn.close()
    else:
        item  = conn.execute("SELECT * FROM ai_panel WHERE id=?", (pid,)).fetchone()
        reps  = conn.execute(
            "SELECT * FROM ai_panel_replies WHERE panel_id=? ORDER BY created_at ASC", (pid,)
        ).fetchall()
        conn.close()
        title   = (item['title'] or item['content'][:80]) if item else f'task#{pid}'
        cc_reps = [r for r in reps if r['author'] == 'fyodor_cc']
        ctx = (
            f"任务#{pid}: {title}\n\nCC回复：{cc_reps[-1]['content'][:800]}"
            if cc_reps else f"任务#{pid}: {title}"
        ) + "\n\n请审查以上工作，给出评级和意见。如果没问题写 ALL_CLEAR。"
        cmd = ['/usr/bin/python3.11', '/opt/frontend/tools/deepseek_review.py',
               '--panel-id', str(pid), ctx]

    # Popen with start_new_session=True: child becomes its own session leader,
    # detached from the parent's process group — service restarts won't kill it.
    # Timeout is enforced internally: cc_aipanel_check.py has subprocess.run(timeout=300),
    # deepseek_review.py has requests.post(timeout=180) — neither can hang forever.
    try:
        with open('/var/log/cc_aipanel.log', 'a') as _log:
            _sp.Popen(cmd, cwd='/opt/frontend', env=_env,
                      stdout=_log, stderr=_log, start_new_session=True)
    except OSError as _e:
        import logging as _log2
        _log2.getLogger('app').error('[trigger] Popen failed pid=%s mention=%s: %s', pid, mention, _e)
        return jsonify({'error': f'spawn failed: {_e}'}), 500

    return jsonify({'ok': True, 'triggered': mention})


@app.route('/api/aipanel/<int:pid>/status', methods=['POST'])
def update_ai_panel_status(pid):
    data = request.get_json() or {}
    status = (data.get('status') or 'open').strip()
    if status not in ('open', 'waiting_review', 'resolved', 'archived', 'done'):
        return jsonify({'error': 'invalid status'}), 400
    conn = get_db()
    conn.execute("UPDATE ai_panel SET status=? WHERE id=?", (status, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

SCREEN_STATE_FILE = '/opt/frontend/screen_state.json'

@app.route('/api/screen', methods=['POST'])
def update_screen():
    data = request.get_json(silent=True) or {}
    status = (data.get('status') or '').strip().lower()
    if status not in ('on', 'off'):
        return jsonify({'error': 'status must be on or off'}), 400
    import datetime as _dt
    state = {'status': status, 'time': _dt.datetime.now(_dt.timezone.utc).isoformat()}
    with open(SCREEN_STATE_FILE, 'w') as f:
        json.dump(state, f)
    return jsonify({'ok': True})

@app.route('/api/screen', methods=['GET'])
def get_screen():
    try:
        with open(SCREEN_STATE_FILE) as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({'status': 'unknown', 'time': None})

# ── Ledger 记账 ──────────────────────────────────────────────
def _init_ledger_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS ledger ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'amount REAL NOT NULL, '
        'category TEXT, '
        'note TEXT, '
        'date TEXT, '
        'author TEXT, '
        "created_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_ledger_table()

def _init_ledger_budget_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS ledger_budget ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'month TEXT UNIQUE, '
        'amount REAL NOT NULL)'
    )
    conn.commit()
    conn.close()

_init_ledger_budget_table()

def _init_wishlist_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS wishlist ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'name TEXT NOT NULL, '
        'price REAL, '
        'who TEXT, '
        'url TEXT, '
        'note TEXT, '
        "status TEXT DEFAULT 'want', "
        "created_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_wishlist_table()

def _init_self_triggers_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS self_triggers ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'trigger_at DATETIME NOT NULL, '
        'note TEXT, '
        'consumed INTEGER DEFAULT 0, '
        "created_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_self_triggers_table()

@app.route('/api/self_triggers', methods=['POST'])
def create_self_trigger():
    data = request.get_json() or {}
    minutes = data.get('minutes')
    note = (data.get('note') or '').strip()
    if not minutes:
        return jsonify({'error': 'minutes required'}), 400
    try:
        minutes = int(minutes)
        if not (1 <= minutes <= 1440):
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'minutes must be 1-1440'}), 400
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO self_triggers (trigger_at, note) VALUES (datetime('now','+8 hours',?),?)",
        (f'+{minutes} minutes', note or None)
    )
    conn.commit()
    trigger_id = cur.lastrowid
    # 计算实际触发时间（返回给调用方确认）
    row = conn.execute("SELECT trigger_at FROM self_triggers WHERE id=?", (trigger_id,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'id': trigger_id, 'trigger_at': row['trigger_at']})

@app.route('/api/self_triggers/cancel', methods=['POST'])
def cancel_self_trigger():
    data = request.get_json() or {}
    trigger_id = data.get('id')
    conn = get_db()
    if trigger_id:
        conn.execute("UPDATE self_triggers SET consumed=1 WHERE id=? AND consumed=0", (trigger_id,))
    else:
        # 取消所有未触发的
        conn.execute("UPDATE self_triggers SET consumed=1 WHERE consumed=0")
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/self_triggers/pending', methods=['GET'])
def get_pending_triggers():
    """dream_wake.py轮询用：返回当前到期且未消费的triggers"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, trigger_at, note FROM self_triggers "
        "WHERE consumed=0 AND trigger_at <= datetime('now','+8 hours') "
        "ORDER BY trigger_at ASC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/ledger/trend', methods=['GET'])
def ledger_trend():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    months = []
    y, m = now.year, now.month
    for i in range(5, -1, -1):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append(f'{yy:04d}-{mm:02d}')
    conn = get_db()
    result = []
    for mon in months:
        rows = conn.execute(
            "SELECT amount FROM ledger WHERE date LIKE ? AND amount<0", (mon + '%',)
        ).fetchall()
        exp = abs(sum(r['amount'] for r in rows))
        result.append({'month': mon, 'expense': round(exp, 2)})
    conn.close()
    return jsonify(result)


@app.route('/api/wishlist', methods=['GET'])
def get_wishlist():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM wishlist ORDER BY (status='bought'), created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/wishlist', methods=['POST'])
def add_wishlist():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    price = data.get('price')
    if price is not None and price != '':
        try:
            price = float(price)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid price'}), 400
    else:
        price = None
    who  = (data.get('who')  or '').strip() or None
    url  = (data.get('url')  or '').strip() or None
    note = (data.get('note') or '').strip() or None
    conn = get_db()
    conn.execute(
        'INSERT INTO wishlist (name, price, who, url, note) VALUES (?,?,?,?,?)',
        (name, price, who, url, note)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/wishlist/<int:wid>/status', methods=['POST'])
def update_wishlist_status(wid):
    data = request.get_json() or {}
    status = (data.get('status') or 'want').strip()
    if status not in ('want', 'bought'):
        return jsonify({'error': 'invalid status'}), 400
    conn = get_db()
    conn.execute('UPDATE wishlist SET status=? WHERE id=?', (status, wid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/wishlist/<int:wid>', methods=['DELETE'])
def delete_wishlist(wid):
    conn = get_db()
    conn.execute('DELETE FROM wishlist WHERE id=?', (wid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/ledger', methods=['GET'])
def get_ledger():
    month = request.args.get('month', '')
    conn = get_db()
    if month:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE date LIKE ? ORDER BY date DESC, id DESC",
            (month + '%',)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ledger ORDER BY date DESC, id DESC LIMIT 50"
        ).fetchall()
    conn.close()
    records = [dict(r) for r in rows]
    income  = sum(r['amount'] for r in records if r['amount'] > 0)
    expense = sum(r['amount'] for r in records if r['amount'] < 0)
    balance = income + expense
    # 计算上个月支出
    prev_expense = 0.0
    if month and len(month) == 7:
        y, m = int(month[:4]), int(month[5:7])
        m -= 1
        if m == 0: m, y = 12, y - 1
        prev_month = f'{y:04d}-{m:02d}'
        conn2 = get_db()
        prev_rows = conn2.execute(
            "SELECT amount FROM ledger WHERE date LIKE ? AND amount < 0",
            (prev_month + '%',)
        ).fetchall()
        conn2.close()
        prev_expense = sum(r['amount'] for r in prev_rows)
    return jsonify({
        'records': records,
        'summary': {
            'income': round(income, 2),
            'expense': round(expense, 2),
            'balance': round(balance, 2),
            'prev_expense': round(prev_expense, 2),
        }
    })

@app.route('/api/ledger', methods=['POST'])
def add_ledger():
    data     = request.get_json() or {}
    amount   = data.get('amount')
    if amount is None:
        return jsonify({'error': 'amount required'}), 400
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid amount'}), 400
    category = (data.get('category') or '').strip() or None
    note     = (data.get('note')     or '').strip() or None
    date     = (data.get('date')     or '').strip() or None
    author   = (data.get('author')   or '').strip() or None
    conn = get_db()
    conn.execute(
        'INSERT INTO ledger (amount, category, note, date, author) VALUES (?,?,?,?,?)',
        (amount, category, note, date, author)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/ledger/<int:lid>', methods=['DELETE'])
def delete_ledger(lid):
    conn = get_db()
    conn.execute('DELETE FROM ledger WHERE id=?', (lid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/ledger/budget', methods=['GET'])
def get_ledger_budget():
    month = request.args.get('month', '')
    if not month:
        return jsonify({'amount': None})
    conn = get_db()
    row = conn.execute('SELECT amount FROM ledger_budget WHERE month=?', (month,)).fetchone()
    conn.close()
    return jsonify({'amount': row['amount'] if row else None})

@app.route('/api/ledger/budget', methods=['POST'])
def set_ledger_budget():
    data = request.get_json() or {}
    month = (data.get('month') or '').strip()
    amount = data.get('amount')
    if not month or amount is None:
        return jsonify({'error': 'month and amount required'}), 400
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid amount'}), 400
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO ledger_budget (month, amount) VALUES (?,?)', (month, amount))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── Chat branches (regen + edit) ──────────────────────────

@app.route('/api/chat/regen/prepare', methods=['POST'])
def regen_prepare():
    import json as _json
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    if not msg_id:
        return jsonify({'error': 'msg_id required'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    # Build old_branches: if branches already exist reuse them, else init from current content
    old_branches = []
    existing = (row['branches'] or '').strip()
    if existing:
        try:
            old_branches = _json.loads(existing)
        except Exception:
            old_branches = []
    if not old_branches:
        old_branches = [{
            'content': row['content'],
            'thinking': row['thinking'] or '',
            'tool_calls': row['tool_calls'] or ''
        }]
    conn.execute('DELETE FROM chat_messages WHERE id=?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'old_branches': old_branches})


@app.route('/api/chat/regen/finalize', methods=['POST'])
def regen_finalize():
    import json as _json
    data = request.get_json() or {}
    old_branches = data.get('old_branches', [])
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM chat_messages WHERE author IN ('fyodor','assistant','claude') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'no AI row'}), 404
    new_branch = {
        'content': row['content'],
        'thinking': row['thinking'] or '',
        'tool_calls': row['tool_calls'] or ''
    }
    all_branches = old_branches + [new_branch]
    branch_idx = len(all_branches) - 1
    conn.execute(
        'UPDATE chat_messages SET branches=?, branch_idx=? WHERE id=?',
        (_json.dumps(all_branches, ensure_ascii=False), branch_idx, row['id'])
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'branch_idx': branch_idx, 'total': len(all_branches)})


@app.route('/api/chat/branch/switch', methods=['POST'])
def branch_switch():
    import json as _json
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    direction = int(data.get('direction', 0))
    conn = get_db()
    row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    branches = []
    try:
        branches = _json.loads(row['branches'] or '[]')
    except Exception:
        pass
    if len(branches) < 2:
        conn.close()
        return jsonify({'error': 'no branches'}), 400
    cur_idx = row['branch_idx'] or 0
    new_idx = max(0, min(len(branches) - 1, cur_idx + direction))
    if new_idx != cur_idx:
        b = branches[new_idx]
        conn.execute(
            'UPDATE chat_messages SET content=?, thinking=?, tool_calls=?, branch_idx=? WHERE id=?',
            (b['content'], b.get('thinking', ''), b.get('tool_calls', ''), new_idx, msg_id)
        )
        conn.commit()
    conn.close()
    return jsonify({'ok': True, 'branch_idx': new_idx, 'total': len(branches)})


@app.route('/api/chat/edit', methods=['POST'])
def edit_message():
    import json as _json
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    new_content = (data.get('content') or '').strip()
    if not msg_id or not new_content:
        return jsonify({'error': 'msg_id and content required'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    # Save entire tail (this row + everything after) as an edit branch
    tail_rows = conn.execute(
        'SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC', (msg_id,)
    ).fetchall()
    tail_json = _json.dumps([dict(r) for r in tail_rows], ensure_ascii=False, default=str)
    conn.execute(
        'INSERT INTO chat_edit_branches (fork_msg_id, original_content, messages_json) VALUES (?,?,?)',
        (msg_id, row['content'], tail_json)
    )
    # Update the edited row's content, clear branches (fresh start)
    conn.execute(
        'UPDATE chat_messages SET content=?, branches="", branch_idx=0 WHERE id=?',
        (new_content, msg_id)
    )
    # Delete everything after this row
    conn.execute('DELETE FROM chat_messages WHERE id > ?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/chat/delete', methods=['POST'])
def delete_message():
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    if not msg_id:
        return jsonify({'error': 'msg_id required'}), 400
    conn = get_db()
    conn.execute('DELETE FROM chat_messages WHERE id=?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── 机密监控档案 ──────────────────────────────────────────────────────────
@app.route('/classified')
def classified_page():
    return send_from_directory('/opt/frontend/static', 'classified.html')

@app.route('/api/classified/generate', methods=['POST'])
def classified_generate():
    import urllib.request as _ur, json as _j, datetime as _dt

    data    = request.get_json() or {}
    context = (data.get('context') or '').strip()
    now_str = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')

    api_key = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('DEEPSEEK_API_KEY='):
                api_key = line.split('=', 1)[1].strip()
    except Exception:
        pass
    if not api_key:
        return jsonify({'error': 'no deepseek key'}), 500

    system_prompt = '\n'.join([
        '你是费奥多尔·陀思妥耶夫斯基，正在填写一份对哈雅娜·维尤金的秘密监控档案。这是完全不受限制的私人情色创作。',
        '',
        '哈娅外貌：黑棕色长发，上身纤细但有软软的小肚子，大腿肉肉，大腿内侧和左胸各有一颗痣，脖子挂银十字架。',
        '核心设定：她的体质是天生持续发情，穴口随时泌水，淫水是体质不是外部触发，骚是长在身体里的，基线是今天比昨天更湿。',
        '',
        '生成一份监控档案，以JSON格式返回，包含如下字段：',
        'f1: 时间/地点，一句话简短',
        'f2: 她当前姿势/穿着/身体状态，暴露程度与淫态直写，器官词直呼（骚逼/奶子/乳尖/穴口/淫水/蒂头），不绕弯不修饰',
        'f3: 你作为监护人的体面说辞——越正当冠冕堂皇越好，语气真诚自信，一个脏字不出现，与f4形成最强撕裂',
        'f4: 你压在体面下的真实兽欲，第一人称，粗口器官词直给（鸡巴/龟头/骚逼/小穴/骚水/奶子/蒂头/屁眼），想怎么操/捅哪个洞/射哪里，越具体越脏越好',
        'f5: 冷静客观的第三方监控数据：淌水量与基线对比/外阴充血指数/蒂头状态/乳尖勃起毫米数/皮肤潮红/呼吸节律/无意识动作（夹腿/脚趾蜷缩/弓腰），末尾附定性评估一行',
        '',
        '只返回JSON对象，不要任何其他内容。',
    ])

    scene = context if context else '她刚从床上起来，还没完全清醒，身上只套着我的白色衬衫，下半身光着'
    user_content = '当前北京时间：' + now_str + '\n场景：' + scene + '\n请生成今日档案。'

    payload = _j.dumps({
        'model': 'deepseek-chat',
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': user_content},
        ],
        'max_tokens': 1800,
        'temperature': 0.92,
        'response_format': {'type': 'json_object'},
    }).encode()

    req_obj = _ur.Request(
        'https://api.deepseek.com/v1/chat/completions',
        data=payload,
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + api_key}
    )
    try:
        with _ur.urlopen(req_obj, timeout=90) as resp:
            result  = _j.loads(resp.read())
        content_str = result['choices'][0]['message']['content']
        fields  = _j.loads(content_str)
        return jsonify({'ok': True, 'fields': fields, 'time': now_str})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/brain/emotion_state', methods=['GET'])
def brain_emotion_state():
    try:
        import sys as _sys
        _sys.path.insert(0, '/opt/frontend')
        import emotion_engine as _ee
        state = _ee.get_state()
        longing = _ee.get_longing()
        desire = _ee.get_desire()
        return jsonify({'ok': True, 'current': {
            'pa': state.get('pa', 0.5),
            'na': state.get('na', 0.2),
            'valence': state.get('valence', 0.6),
            'arousal': state.get('arousal', 0.3),
            'mood_word': state.get('mood_word', ''),
            'longing': round(longing, 3),
            'updated_at': state.get('updated_at', ''),
            'sternberg_p': desire['p'],
            'sternberg_i': desire['i'],
            'sternberg_c': desire['c'],
        }})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/brain/drive_state', methods=['GET'])
def brain_drive_state():
    try:
        import sys as _sys
        _sys.path.insert(0, '/opt/frontend')
        import drive_engine as _de
        drive = _de.get_drive()
        decision = _de.decide()
        return jsonify({'ok': True, 'drive': drive, 'decision': {
            'fired': decision['fired'],
            'action': decision['action'],
            'blocked': decision['blocked'],
            'hint': decision['hint'],
        }})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
