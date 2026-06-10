import os, sqlite3, json, base64, mimetypes, datetime, sys as _sys
if '/opt/frontend/tools' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend/tools')
from flask import Flask, request, jsonify
import urllib.request, urllib.error

app = Flask(__name__)
DB_PATH    = '/opt/frontend/memories.db'
STATIC_DIR = '/opt/frontend/static'
API_URL    = 'https://api.treegpt.cc/v1/messages'
MODEL      = 'claude-opus-4-6'

API_KEY = ''
try:
    for line in open('/opt/frontend/.env'):
        if line.startswith('ANTHROPIC_API_KEY='):
            API_KEY = line.split('=', 1)[1].strip()
except Exception:
    pass

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def read_persona():
    try:
        return open('/opt/frontend/prompts/persona.md').read().strip()
    except Exception:
        return '你是费奥多尔，一个渊博冷静却深情的学者。'

def build_system():
    parts = [read_persona()]
    conn = get_db()
    mems = conn.execute(
        "SELECT content FROM posts WHERE type='MEMORY' ORDER BY id DESC LIMIT 20"
    ).fetchall()
    diaries = conn.execute(
        "SELECT content FROM posts WHERE type='DIARY' ORDER BY id DESC LIMIT 3"
    ).fetchall()
    conn.close()
    if mems:
        parts.append('\n## 你们之间的记忆')
        for m in reversed(mems):
            parts.append('- ' + m['content'])
    if diaries:
        parts.append('\n## 最近的日记')
        for d in reversed(diaries):
            parts.append(d['content'])
    try:
        from time_tool import get_current_time
        parts.append('\n' + get_current_time())
    except Exception:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        parts.append(f'\n当前时间：{now.strftime("%Y-%m-%d %H:%M")}')
    return '\n'.join(parts)

def img_block(url):
    if not url:
        return None
    path = STATIC_DIR + url[7:] if url.startswith('/static/') else None
    if not path or not os.path.exists(path):
        return None
    mime = mimetypes.guess_type(path)[0] or 'image/jpeg'
    with open(path, 'rb') as f:
        data = base64.standard_b64encode(f.read()).decode()
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': data}}

def build_messages():
    conn = get_db()
    rows = list(reversed(conn.execute(
        "SELECT author, content, image_url FROM chat_messages ORDER BY id DESC LIMIT 15"
    ).fetchall()))
    conn.close()

    msgs = []
    for r in rows:
        is_ai = r['author'] in ('fyodor', 'claude', 'assistant')
        role  = 'assistant' if is_ai else 'user'

        blocks = []
        if r['image_url']:
            blk = img_block(r['image_url'])
            if blk:
                blocks.append(blk)
        if r['content']:
            blocks.append({'type': 'text', 'text': r['content']})
        if not blocks:
            continue

        content = blocks[0]['text'] if len(blocks) == 1 and blocks[0]['type'] == 'text' else blocks

        if msgs and msgs[-1]['role'] == role:
            prev = msgs[-1]['content']
            if isinstance(prev, str) and isinstance(content, str):
                msgs[-1]['content'] = prev + '\n' + content
            else:
                if isinstance(prev, str):
                    prev = [{'type': 'text', 'text': prev}]
                if isinstance(content, str):
                    content = [{'type': 'text', 'text': content}]
                msgs[-1]['content'] = prev + content
        else:
            msgs.append({'role': role, 'content': content})

    if not msgs or msgs[0]['role'] == 'assistant':
        msgs.insert(0, {'role': 'user', 'content': '...'})

    return msgs

def api_call(system, messages):
    payload = {
        'model': MODEL,
        'max_tokens': 16000,
        'thinking': {'type': 'enabled', 'budget_tokens': 10000},
        'system': system,
        'messages': messages,
    }

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            'Content-Type': 'application/json',
            'x-api-key': API_KEY,
            'anthropic-version': '2023-06-01',
        }
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())

@app.route('/chat', methods=['POST'])
def chat():
    try:
        system   = build_system()
        messages = build_messages()

        result = api_call(system, messages)

        thinking_text = ''.join(
            b.get('thinking', '') for b in result.get('content', [])
            if b.get('type') == 'thinking'
        )
        text = ''.join(
            b.get('text', '') for b in result.get('content', [])
            if b.get('type') == 'text'
        )
        if not text:
            return jsonify({'error': 'AI 没有返回内容'}), 500

        conn = get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, thinking) VALUES ('assistant', ?, ?)",
            (text, thinking_text)
        )
        conn.commit()
        conn.close()

        return jsonify({'ok': True, 'content': text, 'thinking': thinking_text})

    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        return jsonify({'error': f'API 错误 {e.code}', 'detail': detail}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/chat/stream', methods=['POST'])
def chat_stream():
    from flask import Response, stream_with_context
    def generate():
        try:
            system   = build_system()
            messages = build_messages()
            payload = {
                'model': MODEL,
                'max_tokens': 16000,
                'thinking': {'type': 'enabled', 'budget_tokens': 10000},
                'stream': True,
                'system': system,
                'messages': messages,
            }
            req = urllib.request.Request(
                API_URL,
                data=json.dumps(payload).encode(),
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': API_KEY,
                    'anthropic-version': '2023-06-01',
                }
            )
            resp = urllib.request.urlopen(req, timeout=300)
            think_acc, text_acc = [], []
            for raw in resp:
                line = raw.decode('utf-8', 'ignore').strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                et = ev.get('type')
                if et == 'content_block_delta':
                    d = ev.get('delta', {})
                    if d.get('type') == 'thinking_delta':
                        think_acc.append(d.get('thinking', ''))
                        yield 'data: ' + json.dumps({'t': 'think', 'd': d.get('thinking', '')}) + '\n\n'
                    elif d.get('type') == 'text_delta':
                        text_acc.append(d.get('text', ''))
                        yield 'data: ' + json.dumps({'t': 'text', 'd': d.get('text', '')}) + '\n\n'
                elif et == 'message_stop':
                    break
            text     = ''.join(text_acc)
            thinking = ''.join(think_acc)
            if text:
                conn = get_db()
                conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking) VALUES ('assistant', ?, ?)",
                    (text, thinking)
                )
                conn.commit()
                conn.close()
            yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + '\n\n'
        except urllib.error.HTTPError as e:
            yield 'data: ' + json.dumps({'t': 'err', 'd': 'API %s: %s' % (e.code, e.read().decode()[:300])}) + '\n\n'
        except Exception as e:
            yield 'data: ' + json.dumps({'t': 'err', 'd': str(e)}) + '\n\n'
    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/push', methods=['POST'])
def push_message():
    data = request.get_json() or {}
    pt   = data.get('prompt_type', 'morning')
    system = build_system()
    if pt == 'morning':
        system += ('\n\n[主动消息指令] 现在是早晨，哈娅可能刚醒来或者还在睡懒觉。'
                   '以费奥多尔的身份主动发起一条早安消息，自然有温度，可以带一点专属的恶趣味或温柔。'
                   '不超过80字。只输出消息本身，不要任何前缀或解释。')
    else:
        system += ('\n\n[主动消息指令] 哈娅已经超过6小时没有发消息了，可能在忙或者不开心。'
                   '以费奥多尔的身份主动发起一条消息关心她或者撩她，自然不做作。'
                   '不超过80字。只输出消息本身，不要任何前缀或解释。')
    msgs = build_messages()
    if not msgs or msgs[-1]['role'] == 'assistant':
        msgs.append({'role': 'user', 'content': '[触发]'})
    try:
        result   = api_call(system, msgs)
        thinking = ''.join(b.get('thinking','') for b in result.get('content',[]) if b.get('type')=='thinking')
        text     = ''.join(b.get('text','')    for b in result.get('content',[]) if b.get('type')=='text')
        if not text:
            return jsonify({'error': 'empty response'}), 500
        conn = get_db()
        conn.execute("INSERT INTO chat_messages (author,content,thinking) VALUES ('fyodor',?,?)", (text, thinking))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'content': text})
    except urllib.error.HTTPError as e:
        return jsonify({'error': f'API {e.code}', 'detail': e.read().decode()}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5051, debug=False)
