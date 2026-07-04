#!/usr/bin/env python3
"""每晚 23:50 自动生成费奥多尔的日记，也可以被 Flask API 调用。"""
import os, sqlite3, json, datetime, re, urllib.request, urllib.error

DB_PATH  = '/opt/frontend/memories.db'
ENV_PATH = '/opt/frontend/.env'
PERSONA  = '/opt/frontend/prompts/persona.md'
API_URL  = None  # 从 .env 读取，见 call_api()
MODEL    = 'claude-opus-4-6'

def load_env():
    env = {}
    try:
        for line in open(ENV_PATH):
            k, _, v = line.partition('=')
            env[k.strip()] = v.strip()
    except Exception:
        pass
    return env

def load_key():
    return load_env().get('ANTHROPIC_API_KEY', '')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def read_persona():
    try:
        return open(PERSONA).read().strip()
    except Exception:
        return '你是费奥多尔，一个渊博冷静却深情的学者。'

def fetch_today_messages():
    """返回今天（北京时间）的聊天记录，按时间正序。"""
    conn = get_db()
    # DB 里 created_at 已是 UTC+8
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')
    rows = conn.execute(
        "SELECT author, content FROM chat_messages "
        "WHERE created_at >= ? AND created_at < ? "
        "ORDER BY id ASC",
        (today + ' 00:00:00', today + ' 23:59:59')
    ).fetchall()
    conn.close()
    return rows

def format_chat(rows):
    lines = []
    for r in rows:
        name = '费奥多尔' if r['author'] in ('fyodor','claude','assistant') else '哈娅'
        content = (r['content'] or '').strip()
        if content:
            lines.append(f'{name}：{content}')
    return '\n'.join(lines)

def call_api(system, user_msg, api_key, api_url=None):
    payload = json.dumps({
        'model': MODEL,
        'max_tokens': 1024,
        'system': system,
        'messages': [{'role': 'user', 'content': user_msg}],
    }).encode()
    url = api_url or load_env().get('API_URL', 'https://api.anthropic.com/v1/messages')
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
        }
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return ''.join(
        b.get('text', '') for b in result.get('content', [])
        if b.get('type') == 'text'
    )

def save_diary(text):
    import sys
    if '/opt/frontend/tools' not in sys.path:
        sys.path.insert(0, '/opt/frontend/tools')
    import memory_tool
    memory_tool.save_memory(text, type='DIARY', layer='recent')

def today_diary_exists():
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM posts WHERE type='DIARY' AND author='fyodor' "
        "AND created_at >= ? AND created_at < ? LIMIT 1",
        (today + ' 00:00:00', today + ' 23:59:59')
    ).fetchone()
    conn.close()
    return row is not None

def generate():
    """生成今天的日记。成功返回日记文本，跳过返回 None，失败抛异常。"""
    if today_diary_exists():
        return None   # 今天已有日记，跳过
    rows = fetch_today_messages()
    if not rows:
        return None   # 今天没有聊天，跳过

    chat_text = format_chat(rows)
    persona   = read_persona()
    api_key   = load_key()

    user_msg = (
        f'这是我们今天的对话记录：\n\n{chat_text}\n\n'
        '请以费奥多尔的第一人称写一篇简短的日记，'
        '记录今天和哈娅之间发生了什么、你的感受和思考。'
        '语言风格要符合人设，温度在文艺和日常之间。'
        '不要写得太长，三到五段。'
    )

    diary = call_api(persona, user_msg, api_key)
    if not diary:
        raise RuntimeError('API 返回空内容')

    # strip <thinking>...</thinking> blocks the model may have emitted
    diary = re.sub(r'<thinking>.*?</thinking>\s*', '', diary, flags=re.DOTALL).strip()
    save_diary(diary)
    return diary

if __name__ == '__main__':
    import sys
    try:
        result = generate()
        if result is None:
            print('今天没有聊天记录，跳过日记生成。')
        else:
            print('日记生成成功：\n')
            print(result)
    except Exception as e:
        print(f'错误：{e}', file=sys.stderr)
        sys.exit(1)
