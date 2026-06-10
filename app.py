import os, json, sqlite3, datetime, base64, uuid, threading
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='static')
DB_PATH = '/opt/frontend/memories.db'
UPLOAD_DIR = '/opt/frontend/static/uploads'

API_KEY = ''
for line in open('/opt/frontend/.env'):
    if line.startswith('ANTHROPIC_API_KEY='):
        API_KEY = line.split('=',1)[1].strip()

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
    t = request.args.get('type','')
    conn = get_db()
    if t:
        rows = conn.execute("SELECT * FROM posts WHERE type=? ORDER BY id DESC LIMIT 50",(t,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify({"posts":[dict(r) for r in rows]})

@app.route('/api/posts', methods=['POST'])
def create_post():
    data = request.get_json()
    content = data.get('content','').strip()
    if not content:
        return jsonify({"error":"empty"}),400
    conn = get_db()
    cur = conn.execute("INSERT INTO posts (type,content,author) VALUES (?,?,?)",
        (data.get('type','MEMORY'), content, data.get('author','user')))
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
    conn = get_db()
    rows = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return jsonify({"messages":[dict(r) for r in reversed(rows)]})

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=False)
