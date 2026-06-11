import os, re, sqlite3, json, base64, mimetypes, datetime, sys as _sys
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
GW_PROVIDER = 'treegpt'
CC_TOKEN = ''
try:
    for line in open('/opt/frontend/.env'):
        if line.startswith('ANTHROPIC_API_KEY='):
            API_KEY = line.split('=', 1)[1].strip()
        elif line.startswith('GW_PROVIDER='):
            GW_PROVIDER = line.split('=', 1)[1].strip() or 'treegpt'
        elif line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
            CC_TOKEN = line.split('=', 1)[1].strip()
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
    parts.append(NL + '（你拥有真实的工具：保存与搜索记忆、控制次卧的灯。当下自然需要时安静使用，不必每次提及。）')
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
        "SELECT author, content, image_url, created_at FROM chat_messages ORDER BY id DESC LIMIT 15"
    ).fetchall()))
    conn.close()

    msgs = []
    prev_dt = None
    for r in rows:
        is_ai = r['author'] in ('fyodor', 'claude', 'assistant')
        role  = 'assistant' if is_ai else 'user'

        note = ''
        cur_dt = None
        try:
            cur_dt = datetime.datetime.strptime(r['created_at'], '%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
        if cur_dt and prev_dt and not is_ai:
            gap = cur_dt - prev_dt
            if gap >= datetime.timedelta(minutes=30):
                hrs, rem = divmod(int(gap.total_seconds()), 3600)
                mins = rem // 60
                gap_str = ('%d小时%d分' % (hrs, mins)) if hrs else ('%d分钟' % mins)
                note = '[%s · 距上一条消息隔了%s] ' % (cur_dt.strftime('%m月%d日 %H:%M'), gap_str)
        if cur_dt:
            prev_dt = cur_dt

        blocks = []
        if r['image_url']:
            blk = img_block(r['image_url'])
            if blk:
                blocks.append(blk)
        if r['content']:
            blocks.append({'type': 'text', 'text': note + r['content']})
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
        'tools': TOOLS,
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


NL = chr(10)
SAVE_RE = re.compile(r'\[\[SAVE:\s*(.*?)\]\]', re.DOTALL)
SSE_END = NL + NL

TOOLS = [
    {
        'name': 'save_memory',
        'description': '把对话中重要的信息存入长期记忆（哈娅提到的事件、约定、喜好、重要日期等）。在她说了值得记住的事时安静地使用。',
        'input_schema': {'type': 'object', 'properties': {'content': {'type': 'string', 'description': '要记住的内容，一句话概括'}}, 'required': ['content']},
    },
    {
        'name': 'search_memories',
        'description': '在长期记忆中按关键词搜索，找回更久之前的记忆。当她提到过去的事而你不确定细节时使用。',
        'input_schema': {'type': 'object', 'properties': {'keyword': {'type': 'string'}}, 'required': ['keyword']},
    },
    {'name': 'light_on', 'description': '打开次卧的灯（哈娅的房间）。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_off', 'description': '关闭次卧的灯。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'set_brightness', 'description': '设置次卧灯的亮度。', 'input_schema': {'type': 'object', 'properties': {'value': {'type': 'integer', 'description': '亮度 1-100'}}, 'required': ['value']}},
    {'name': 'set_color_temp', 'description': '设置次卧灯的色温，单位K，2700暖光~6500冷光。', 'input_schema': {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}},
    {'name': 'get_light_status', 'description': '查询次卧灯当前的开关、亮度、色温。', 'input_schema': {'type': 'object', 'properties': {}}},
]

LIGHT_DAEMON_URL = 'http://127.0.0.1:5052'

def run_tool(name, args):
    try:
        if name == 'save_memory':
            import memory_tool
            memory_tool.save_memory(args.get('content', ''))
            return '已存入记忆'
        if name == 'search_memories':
            import memory_tool
            res = memory_tool.search_memories(args.get('keyword', ''))
            if not res:
                return '没有找到相关记忆'
            return NL.join('[%s] %s' % (r.get('created_at', ''), r.get('content', '')) for r in res[:10])
        light_paths = {
            'light_on':         ('/light/on', 'POST', None),
            'light_off':        ('/light/off', 'POST', None),
            'set_brightness':   ('/light/brightness', 'POST', {'value': args.get('value', 50)}),
            'set_color_temp':   ('/light/color_temp', 'POST', {'value': args.get('value', 4000)}),
            'get_light_status': ('/light/status', 'GET', None),
        }
        if name in light_paths:
            path, method, body = light_paths[name]
            data = json.dumps(body).encode() if body else (b'{}' if method == 'POST' else None)
            req = urllib.request.Request(LIGHT_DAEMON_URL + path, data=data, method=method,
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode()
        return '未知工具: ' + name
    except Exception as e:
        return '工具执行失败: ' + str(e)

CC_CWD = '/opt/frontend/.claude-gw'

def messages_to_text(messages):
    lines = []
    for m in messages:
        who = '费奥多尔' if m['role'] == 'assistant' else '哈娅'
        c = m['content']
        if isinstance(c, list):
            txt = ' '.join(b.get('text', '') for b in c
                           if isinstance(b, dict) and b.get('type') == 'text')
            if any(isinstance(b, dict) and b.get('type') == 'image' for b in c):
                txt = '[发来一张图片] ' + txt
        else:
            txt = c
        lines.append(who + '：' + txt)
    return NL.join(lines)

def claude_code_call(system, messages):
    import subprocess
    if not CC_TOKEN:
        raise RuntimeError('未配置订阅 token，请先在 api 设置页填入')
    os.makedirs(CC_CWD, exist_ok=True)
    # Inject save_memory pseudo-tool instruction
    save_instr = (NL + NL
        + '【记忆存储】当你认为对话中出现了值得长期记住的信息时，'
        + '在回复正文的最后另起一行，写一个或多个 [[SAVE: 内容]] 标记，'
        + '用一句话概括要保存的内容。这些标记会被自动处理，不会显示给哈娅。'
        + '正文本身不要提及"我已记录"之类的话。')
    full_system = system + save_instr
    convo = messages_to_text(messages)
    prompt = ('以下是你们最近的对话记录：' + NL + NL + convo + NL + NL
              + '请以费奥多尔的身份自然地回复最后一条消息。只输出回复内容本身，不要任何前缀。')
    env = dict(os.environ)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = CC_TOKEN
    env.pop('ANTHROPIC_API_KEY', None)
    r = subprocess.run(
        ['claude', '-p', prompt, '--output-format', 'json',
         '--system-prompt', full_system, '--max-turns', '3'],
        capture_output=True, text=True, timeout=300, cwd=CC_CWD, env=env
    )
    if r.returncode != 0:
        raise RuntimeError('claude code 调用失败: ' + (r.stderr or r.stdout)[:300])
    d = json.loads(r.stdout)
    if d.get('is_error'):
        raise RuntimeError('claude code 返回错误: ' + str(d.get('result', ''))[:300])
    raw = (d.get('result') or '').strip()
    # Extract [[SAVE: ...]] markers and persist
    saves = SAVE_RE.findall(raw)
    if saves:
        try:
            import memory_tool
            for item in saves:
                item = item.strip()
                if item:
                    memory_tool.save_memory(item)
        except Exception:
            pass
    # Strip markers from displayed text
    text = SAVE_RE.sub('', raw).strip()
    return text, ''

def generate_reply(system, messages):
    if GW_PROVIDER == 'claude_code':
        return claude_code_call(system, messages)
    return agent_loop(system, messages)

def agent_loop(system, messages, max_rounds=5):
    msgs = list(messages)
    think_parts, text_parts = [], []
    for _ in range(max_rounds):
        result = api_call(system, msgs)
        blocks = result.get('content', [])
        for b in blocks:
            if b.get('type') == 'thinking':
                think_parts.append(b.get('thinking', ''))
            elif b.get('type') == 'text':
                text_parts.append(b.get('text', ''))
        tool_uses = [b for b in blocks if b.get('type') == 'tool_use']
        if result.get('stop_reason') != 'tool_use' or not tool_uses:
            break
        msgs.append({'role': 'assistant', 'content': blocks})
        msgs.append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': t.get('id'),
             'content': run_tool(t.get('name', ''), t.get('input') or {})}
            for t in tool_uses
        ]})
    return NL.join(t for t in text_parts if t).strip(), ''.join(think_parts)

@app.route('/chat', methods=['POST'])
def chat():
    try:
        system   = build_system()
        messages = build_messages()

        text, thinking_text = generate_reply(system, messages)
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
    if GW_PROVIDER == 'claude_code':
        def gen_cc():
            try:
                system   = build_system()
                messages = build_messages()
                text, _ = claude_code_call(system, messages)
                if text:
                    yield 'data: ' + json.dumps({'t': 'text', 'd': text}) + SSE_END
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO chat_messages (author, content, thinking) VALUES ('assistant', ?, ?)",
                        (text, '')
                    )
                    conn.commit()
                    conn.close()
                yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
            except Exception as e:
                yield 'data: ' + json.dumps({'t': 'err', 'd': str(e)}) + SSE_END
        return Response(stream_with_context(gen_cc()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    def generate():
        try:
            system   = build_system()
            messages = build_messages()
            think_acc, text_acc = [], []
            for _round in range(5):
                payload = {
                    'model': MODEL,
                    'max_tokens': 16000,
                    'thinking': {'type': 'enabled', 'budget_tokens': 10000},
                    'stream': True,
                    'tools': TOOLS,
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
                blocks, cur, stop_reason = [], None, None
                for raw in resp:
                    line = raw.decode('utf-8', 'ignore').strip()
                    if not line.startswith('data:'):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    et = ev.get('type')
                    if et == 'content_block_start':
                        cb  = ev.get('content_block', {}) or {}
                        cur = {'type': cb.get('type')}
                        if cur['type'] == 'tool_use':
                            cur['id'] = cb.get('id')
                            cur['name'] = cb.get('name')
                            cur['_json'] = ''
                        elif cur['type'] == 'thinking':
                            cur['thinking'] = ''
                        elif cur['type'] == 'text':
                            cur['text'] = ''
                    elif et == 'content_block_delta':
                        d  = ev.get('delta', {})
                        dt = d.get('type')
                        if dt == 'thinking_delta':
                            s = d.get('thinking', '')
                            if cur is not None: cur['thinking'] = cur.get('thinking', '') + s
                            think_acc.append(s)
                            yield 'data: ' + json.dumps({'t': 'think', 'd': s}) + SSE_END
                        elif dt == 'text_delta':
                            s = d.get('text', '')
                            if cur is not None: cur['text'] = cur.get('text', '') + s
                            text_acc.append(s)
                            yield 'data: ' + json.dumps({'t': 'text', 'd': s}) + SSE_END
                        elif dt == 'input_json_delta':
                            if cur is not None: cur['_json'] = cur.get('_json', '') + d.get('partial_json', '')
                        elif dt == 'signature_delta':
                            if cur is not None: cur['signature'] = cur.get('signature', '') + d.get('signature', '')
                    elif et == 'content_block_stop':
                        if cur is not None:
                            if cur.get('type') == 'tool_use':
                                try:
                                    cur['input'] = json.loads(cur.pop('_json') or '{}')
                                except Exception:
                                    cur['input'] = {}
                            blocks.append(cur)
                            cur = None
                    elif et == 'message_delta':
                        stop_reason = (ev.get('delta', {}) or {}).get('stop_reason') or stop_reason
                    elif et == 'message_stop':
                        break
                tool_uses = [b for b in blocks if b.get('type') == 'tool_use']
                if stop_reason != 'tool_use' or not tool_uses:
                    break
                messages.append({'role': 'assistant', 'content': blocks})
                results = []
                for tu in tool_uses:
                    yield 'data: ' + json.dumps({'t': 'tool', 'd': tu.get('name', '')}) + SSE_END
                    results.append({'type': 'tool_result', 'tool_use_id': tu.get('id'),
                                    'content': run_tool(tu.get('name', ''), tu.get('input') or {})})
                messages.append({'role': 'user', 'content': results})
                text_acc.append(NL)
            text     = ''.join(text_acc).strip()
            thinking = ''.join(think_acc)
            if text:
                conn = get_db()
                conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking) VALUES ('assistant', ?, ?)",
                    (text, thinking)
                )
                conn.commit()
                conn.close()
            yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
        except urllib.error.HTTPError as e:
            yield 'data: ' + json.dumps({'t': 'err', 'd': 'API %s: %s' % (e.code, e.read().decode()[:300])}) + SSE_END
        except Exception as e:
            yield 'data: ' + json.dumps({'t': 'err', 'd': str(e)}) + SSE_END
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
        text, thinking = generate_reply(system, msgs)
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


@app.route('/test', methods=['POST'])
def test_send():
    import time as _time
    data = request.get_json() or {}
    message = (data.get('message') or '').strip()
    inject_memory = data.get('inject_memory', True)
    if not message:
        return jsonify({'error': 'message is required'}), 400
    try:
        t0 = _time.time()
        if inject_memory:
            system = build_system()
        else:
            system = '你是一个 AI 助手，请如实回答。'
        if GW_PROVIDER == 'claude_code':
            text, think = claude_code_call(system, [{'role': 'user', 'content': message}])
            latency_ms = int((_time.time() - t0) * 1000)
            return jsonify({
                'content': text, 'thinking': think,
                'latency_ms': latency_ms,
                'input_tokens': 0, 'output_tokens': 0,
                'provider': 'claude_code',
            })
        payload = {
            'model': MODEL,
            'max_tokens': 4096,
            'thinking': {'type': 'enabled', 'budget_tokens': 5000},
            'system': system,
            'messages': [{'role': 'user', 'content': message}],
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
            result = json.loads(resp.read())
        latency_ms = int((_time.time() - t0) * 1000)
        blocks = result.get('content', [])
        text  = NL.join(b.get('text', '') for b in blocks if b.get('type') == 'text').strip()
        think = ''.join(b.get('thinking', '') for b in blocks if b.get('type') == 'thinking')
        usage = result.get('usage', {})
        return jsonify({
            'content':       text,
            'thinking':      think,
            'latency_ms':    latency_ms,
            'input_tokens':  usage.get('input_tokens', 0),
            'output_tokens': usage.get('output_tokens', 0),
        })
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        return jsonify({'error': f'API 错误 {e.code}', 'detail': detail}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5051, debug=False)
