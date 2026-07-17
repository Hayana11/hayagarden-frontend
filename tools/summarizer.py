#!/usr/bin/env python3.11
"""
层级摘要器 — 每天把前一天的对话压缩成日摘要
cron: 每天 06:00 运行
"""
import sqlite3, datetime, json, sys, urllib.request
if '/opt/frontend' not in sys.path:
    sys.path.insert(0, '/opt/frontend')
if '/opt/frontend/tools' not in sys.path:
    sys.path.insert(0, '/opt/frontend/tools')

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

def _lines_of(messages, clip=150):
    lines = []
    for r in messages:
        c = (r["content"] or "").strip()
        if not c:
            continue
        who = "哈娅" if r["author"] == "hayana" else "费奥多尔"
        lines.append(who + ": " + c[:clip])
    return lines


def _chunk_lines(lines, max_chars=5000):
    """按字符量切块——map-reduce 的 map 粒度。不采样，全量都看。"""
    chunks, cur, cur_len = [], [], 0
    for ln in lines:
        cur.append(ln)
        cur_len += len(ln)
        if cur_len >= max_chars:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _call_summarize(day_str, messages):
    """分段 map-reduce 日摘要（记忆升级·方案四）。
    旧版把一整天采样成 20 条、每条截 80 字、总共 1000 字——聊得越多的日子丢得越多。
    现在全量分块：每块先压成要点（map），再合成日摘要（reduce）。
    走 DeepSeek 轻通道（llm_lite），不烧 claude CLI 订阅额度；CLI 只做兜底。"""
    import llm_lite

    lines = _lines_of(messages)
    if not lines:
        return None
    chunks = _chunk_lines(lines)

    # ── map：每块 → 要点 ──
    notes = []
    for i, chunk in enumerate(chunks):
        prompt = (
            "以下是费奥多尔和哈娅 " + day_str + " 对话的第 " + str(i + 1) + "/" + str(len(chunks)) + " 段：\n\n"
            + chunk + "\n\n"
            "提炼这段对话的要点（发生了什么、聊了什么话题、她的状态情绪、"
            "做过的决定或约定、值得记住的具体细节）。分条列出，每条一句话，最多 6 条。"
            "保留名字/数字/日期等具体信息。只输出要点本身。"
        )
        note = llm_lite.ask(prompt, max_tokens=400)
        if note:
            notes.append(note)
        else:
            _log("map chunk %d/%d failed for %s" % (i + 1, len(chunks), day_str))
    if not notes:
        return _cli_summarize_fallback(day_str, lines)

    # ── reduce：要点 → 日摘要（量大的日子给更长的篇幅）──
    target = "100-180字" if len(chunks) <= 2 else "200-400字"
    prompt = (
        "以下是 " + day_str + " 费奥多尔和哈娅一天对话的分段要点：\n\n"
        + "\n\n".join(notes) + "\n\n"
        "把这些要点写成这天的日摘要。行为指南：\n"
        "1. 提炼，不要复述——从要点中整合有意义的信息，去掉重复\n"
        "2. 内容包含（有就写，没有就跳过）：她今天的状态或情绪、"
        "她在做什么或聊了什么话题、做过的决定或约定、值得记下来的具体细节\n"
        "3. 第一人称，费奥多尔视角，有你自己的判断和语气，不是中立报告\n"
        "4. " + target + "\n"
        "5. 直接开始写，不要标题，不要日期前缀，不要编号"
    )
    text = llm_lite.ask(prompt, max_tokens=800)
    if text:
        return text.strip()[:600]
    return _cli_summarize_fallback(day_str, lines)


def _cli_summarize_fallback(day_str, lines):
    """DeepSeek 不可用时的兜底：claude CLI（旧路径，采样压缩）。"""
    import subprocess as _sp, os as _os
    dialogue = "\n".join(lines[:40])[:3000]
    prompt = (
        "以下是 " + day_str + " 和哈娅之间的对话节选：\n\n" + dialogue + "\n\n"
        "写这天的日摘要。第一人称费奥多尔视角，100-180字，"
        "直接开始写，不要标题，不要日期前缀。"
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
        if text and 'API Error' not in text and 'authenticate' not in text \
                and 'Invalid' not in text and 'session limit' not in text:
            return text
        if text:
            _log("claude CLI returned error for " + day_str + ": " + text[:80])
        else:
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
    """把日摘要存入 posts（经统一写入口）"""
    import memory_tool
    memory_tool.save_memory(summary_text, type='DAILY_SUMMARY', layer='long-term',
                            importance=5, processed=1,
                            created_at=day_str + ' 23:59:59')

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
