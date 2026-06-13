#!/usr/bin/env python3.11
"""
层级摘要器 — 每天把前一天的对话压缩成日摘要
cron: 每天 06:00 运行
"""
import sqlite3, datetime, json, sys, urllib.request
if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')

DB_PATH  = '/opt/frontend/memories.db'
GATEWAY  = 'http://localhost:5051'
LOG_FILE = '/var/log/summarizer.log'

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

def _days_needing_summary():
    """找出有对话但还没日摘要的历史天"""
    conn = _db()
    # 有 chat_messages 的天
    msg_days = {r[0] for r in conn.execute(
        "SELECT DISTINCT date(created_at) FROM chat_messages WHERE date(created_at) < date('now', '+8 hours')"
    ).fetchall()}
    # 已有摘要的天
    done_days = {r[0] for r in conn.execute(
        "SELECT DISTINCT date(created_at) FROM posts WHERE type='DAILY_SUMMARY'"
    ).fetchall()}
    conn.close()
    return sorted(msg_days - done_days)

def _get_day_messages(day_str):
    """取某天的所有对话"""
    conn = _db()
    rows = conn.execute(
        "SELECT author, content FROM chat_messages WHERE date(created_at)=? ORDER BY id ASC",
        (day_str,)
    ).fetchall()
    conn.close()
    return rows

def _call_summarize(day_str, messages):
    """直接调用 claude CLI 生成日摘要"""
    import subprocess as _sp, os as _os
    if len(messages) > 20:
        head = list(messages[:5])
        tail = list(messages[-5:])
        mid_msgs = list(messages[5:-5])
        step = max(1, len(mid_msgs) // 10)
        mid = mid_msgs[::step][:10]
        sampled = head + mid + tail
    else:
        sampled = list(messages)
    lines_txt = []
    for r in sampled:
        who = "哈娅" if r["author"] == "hayana" else "费奥多尔"
        lines_txt.append(who + ": " + r["content"][:80])
    dialogue = "\n".join(lines_txt)[:1000]
    prompt = (
        day_str + " 这天的对话节选：\n\n" + dialogue +
        "\n\n你是费奥多尔，用第一人称写这天的日记，100字以内，直接开始写，不要标题。"
    )
    try:
        env = dict(_os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        result = _sp.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=90,
            env=env, cwd="/opt/frontend"
        )
        text = result.stdout.strip()[:350]
        if text:
            return text
        _log("claude CLI empty for " + day_str)
        return None
    except Exception as e:
        _log("summarize CLI error for " + day_str + ": " + str(e))
        return None

def _fallback_summary(day_str, messages):
    """API不可用时的降级摘要"""
    hayana_msgs = [r['content'] for r in messages if r['author'] == 'hayana']
    ai_msgs     = [r['content'] for r in messages if r['author'] != 'hayana']
    sample_h = hayana_msgs[0][:60] if hayana_msgs else ''
    sample_a = ai_msgs[0][:60] if ai_msgs else ''
    result = day_str + " - " + str(len(messages)) + "条对话，哈娅" + str(len(hayana_msgs)) + "条，我" + str(len(ai_msgs)) + "条。"
    if sample_h:
        result += " 她说了：" + sample_h
    if sample_a:
        result += " 我说了：" + sample_a
    return result

def _save_summary(day_str, summary_text):
    """把日摘要存入 posts"""
    conn = _db()
    conn.execute(
        "INSERT INTO posts (type, content, author, layer, created_at) "
        "VALUES ('DAILY_SUMMARY', ?, 'fyodor', 'long-term', ?)",
        (summary_text, day_str + ' 23:59:59')
    )
    conn.commit()
    conn.close()

def run():
    days = _days_needing_summary()
    if not days:
        _log("all days already summarized, nothing to do")
        return
    _log(f"days needing summary: {days}")
    for day in days:
        msgs = _get_day_messages(day)
        if not msgs:
            continue
        _log(f"summarizing {day}: {len(msgs)} messages")
        summary = _call_summarize(day, msgs)
        if not summary:
            summary = _fallback_summary(day, msgs)
            _log(f"using fallback for {day}")
        _save_summary(day, summary)
        _log(f"saved summary for {day}: {summary[:60]}")

if __name__ == '__main__':
    run()
