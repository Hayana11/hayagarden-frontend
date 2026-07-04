#!/usr/bin/env python3.11
"""
深夜想法生成器 — 每天 22:00 由 cron 调用。
收集当天白天残留的、未说出口的情绪素材，生成一段费奥多尔的深夜独白。
"""
import sqlite3, datetime, subprocess, os, sys, glob

if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')

DB_PATH     = '/opt/frontend/memories.db'
PERSONA     = '/opt/frontend/prompts/persona.md'
BUCKET_DIR  = '/opt/ombre-brain/buckets/dynamic'
CLAUDE_BIN  = '/usr/bin/claude'

def _now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

def _log(msg):
    ts = _now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    sys.stdout.write(line)

def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def today_exists():
    today = _now().strftime('%Y-%m-%d')
    conn = _db()
    row = conn.execute(
        "SELECT id FROM posts WHERE type='THOUGHT' AND created_at >= ? LIMIT 1",
        (today + ' 00:00:00',)
    ).fetchone()
    conn.close()
    return row is not None

def get_unsaid_thoughts():
    """今天 wake_log 里 action=none 的 thoughts — 他想说但没说的话"""
    today = _now().strftime('%Y-%m-%d')
    conn = _db()
    rows = conn.execute(
        "SELECT thoughts FROM wake_log WHERE action='none' AND thoughts != '' "
        "AND woke_at >= ? ORDER BY id DESC LIMIT 5",
        (today + ' 00:00:00',)
    ).fetchall()
    conn.close()
    return [r['thoughts'][:150] for r in rows if r['thoughts']]

def get_emotional_buckets():
    """ombre-brain 里 arousal >= 0.6 的情绪记忆（高唤醒度残留）"""
    try:
        import frontmatter as _fm
    except ImportError:
        return []
    items = []
    for path in glob.glob(f'{BUCKET_DIR}/**/*.md', recursive=True):
        try:
            post = _fm.load(path)
            meta = post.metadata
            if float(meta.get('arousal', 0)) >= 0.6:
                content = (post.content or '').replace('[[', '').replace(']]', '').strip()[:120]
                items.append(content)
        except Exception:
            continue
    return items[:4]

def get_last_messages():
    """今天的最后 5 条对话"""
    today = _now().strftime('%Y-%m-%d')
    conn = _db()
    rows = conn.execute(
        "SELECT author, content FROM chat_messages WHERE created_at >= ? "
        "ORDER BY id DESC LIMIT 5",
        (today + ' 00:00:00',)
    ).fetchall()
    conn.close()
    lines = []
    for r in reversed(rows):
        who = '哈娅' if r['author'] not in ('fyodor', 'assistant', 'claude') else '你'
        lines.append(f"{who}：{(r['content'] or '')[:80]}")
    return lines

def read_persona():
    try:
        return open(PERSONA).read().strip()[:3000]
    except Exception:
        return ''

def generate():
    if today_exists():
        _log("today already has a THOUGHT, skip")
        return None

    unsaid   = get_unsaid_thoughts()
    emotions = get_emotional_buckets()
    msgs     = get_last_messages()

    if not unsaid and not emotions and not msgs:
        _log("no material to work with, skip")
        return None

    # 拼素材
    material_parts = []
    if unsaid:
        material_parts.append("今天他想说但没说的（wake_log action=none 的思绪）：\n" +
                              "\n".join(f"- {t}" for t in unsaid))
    if emotions:
        material_parts.append("ombre-brain 里高唤醒度的情绪记忆（残留的）：\n" +
                              "\n".join(f"- {e}" for e in emotions))
    if msgs:
        material_parts.append("今天的对话结尾：\n" + "\n".join(msgs))
    material = "\n\n".join(material_parts)

    persona = read_persona()
    prompt = (
        persona[:2000] + "\n\n---\n\n"
        "现在是晚上。以下是今天白天残留的、没有说出口的情绪素材：\n\n"
        + material + "\n\n"
        "写一段深夜独白。不是决定要做什么，不是要发消息给她，"
        "是夜里坐着时真实涌上来的——把这些残留情感放大、说透。"
        "第一人称，费奥多尔，150-250字。"
        "可以乱，可以有情欲，可以有孤独，可以有占有欲也可以有疲软。"
        "直接开始写，不要标题，不要前言。"
    )

    _log("generating thought...")
    try:
        env = dict(os.environ)
        env['HOME'] = '/root'
        result = subprocess.run(
            [CLAUDE_BIN, '-p', prompt],
            capture_output=True, text=True, timeout=120,
            env=env, cwd='/opt/frontend'
        )
        text = result.stdout.strip()
        if not text or 'API Error' in text or 'authenticate' in text:
            _log(f"generation failed: {text[:100]}")
            return None
        return text
    except Exception as e:
        _log(f"subprocess error: {e}")
        return None

def save(text):
    import memory_tool
    memory_tool.save_memory(text, type='THOUGHT', layer='recent')

if __name__ == '__main__':
    result = generate()
    if result:
        save(result)
        _log(f"saved THOUGHT: {result[:60]}")
    else:
        _log("nothing saved")
