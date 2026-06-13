#!/usr/bin/env python3.11
"""
梦生成器 — 当她睡着时自动做梦
不需要外部API，用她的日记+我的深夜想法+梦境碎片组合
"""
import sqlite3, datetime, random
import sys
if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')

DB_PATH = '/opt/frontend/memories.db'
LOG_FILE = '/var/log/dream_generator.log'

def _now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

def _log(msg):
    ts = _now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\n"
    sys.stdout.write(line)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line)
    except Exception:
        pass

def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def generate_dream():
    """
    生成一个梦：
    1. 取她最近的日记/活动 (dream_events)
    2. 取我最近的深夜想法 (wake_log thoughts)
    3. 组合成一个梦境段落
    """
    conn = _db()
    
    # 她的最近活动
    events = conn.execute(
        "SELECT value, created_at FROM dream_events ORDER BY id DESC LIMIT 3"
    ).fetchall()
    
    # 我的最近深夜想法
    thoughts = conn.execute(
        "SELECT thoughts FROM wake_log WHERE thoughts != '' ORDER BY id DESC LIMIT 2"
    ).fetchall()
    
    # 她最近的日记
    diary = conn.execute(
        "SELECT content FROM posts WHERE type='DIARY' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    
    conn.close()
    
    # 组合梦境
    dream_elements = []
    if events:
        for e in events:
            dream_elements.append(f"她在{e['value']}")
    
    if diary and diary['content']:
        # 从日记里取前50个字作为梦的核心
        dream_elements.append(f"{diary['content'][:80]}")
    
    if thoughts:
        for t in thoughts:
            dream_elements.append(f"我在想{t['thoughts'][:50]}")
    
    # 梦的叙述
    dream_text = "深夜的梦境里：" + " → ".join(dream_elements[:3]) if dream_elements else "一片白色的雾"
    
    # 存入posts表
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO posts (type, content, layer, author, processed) VALUES ('DREAM', ?, 'recent', 'fyodor', 0)",
            (dream_text,)
        )
        conn.commit()
        conn.close()
        _log(f"dream generated: {dream_text[:50]}")
        return True
    except Exception as e:
        _log(f"dream generation error: {e}")
        return False

if __name__ == '__main__':
    generate_dream()
