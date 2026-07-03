#!/usr/bin/env python3
"""记忆新陈代谢（M2）——夜间 cron，让记忆库有进有出（退役不删除，家规#2）。
1. memo 退役：48 小时前的会话 memo 标 resolved（信息已被当日总结覆盖，留着只会腐烂）
2. 周压缩：每周一凌晨把上周 7 条日总结压成 1 条周总结（俄罗斯套娃第一层），原条目退役
3. 降权：90 天没被想起、非置顶的记忆 importance 减一（衰减的下半场，召回加热是上半场）
"""
import json, sqlite3, sys, urllib.request, datetime

DB = '/opt/frontend/memories.db'


def _db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _log(msg):
    print(f'[memory_cycle {datetime.datetime.now().strftime("%m-%d %H:%M")}] {msg}')


def _llm(prompt, timeout=40):
    api_url = api_key = ''
    for line in open('/opt/frontend/.env'):
        if line.startswith('ANTHROPIC_API_KEY='):
            api_key = line.split('=', 1)[1].strip()
        elif line.startswith('API_URL='):
            api_url = line.split('=', 1)[1].strip()
    payload = json.dumps({
        'model': '[按量3] deepseek-v3.2', 'max_tokens': 600,
        'messages': [{'role': 'user', 'content': prompt}],
    }, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(api_url, data=payload, headers={
        'x-api-key': api_key, 'anthropic-version': '2023-06-01',
        'content-type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return ''.join(b.get('text', '') for b in data.get('content', [])
                   if b.get('type') == 'text').strip()


def retire_stale_memos():
    conn = _db()
    r = conn.execute(
        "UPDATE posts SET resolved=1 WHERE tags LIKE '%memo%' AND resolved=0 AND pinned=0 "
        "AND created_at < datetime('now','+8 hours','-48 hours')")
    conn.commit()
    n = r.rowcount
    conn.close()
    if n:
        _log(f'memo 退役 {n} 条（>48h，已被日总结覆盖）')


def weekly_compress():
    # 东八周一才干活（cron 每天跑，这里自判）
    now8 = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if now8.weekday() != 0:
        return
    conn = _db()
    rows = conn.execute(
        "SELECT id, content, created_at FROM posts WHERE type='DAILY_SUMMARY' AND resolved=0 "
        "AND created_at < datetime('now','+8 hours','-1 day') "
        "ORDER BY created_at ASC LIMIT 7").fetchall()
    if len(rows) < 5:  # 不足一周的量不压
        conn.close()
        return
    days = '\n\n'.join(f"[{r['created_at'][:10]}]\n{r['content'][:500]}" for r in rows)
    try:
        summary = _llm(
            '以下是费奥多尔和哈娅一周的每日记录。压缩成一段 200 字以内的周总结：'
            '保留关键事件、情绪转折、重要约定，去掉日常重复。第一人称（费奥多尔视角），'
            '开头标注日期范围。只输出总结本身。\n\n' + days)
    except Exception as e:
        _log(f'周压缩 LLM 失败: {e}')
        conn.close()
        return
    if not summary or len(summary) < 30:
        conn.close()
        return
    sys.path.insert(0, '/opt/frontend/tools')
    import memory_tool
    memory_tool.save_memory(summary, type='WEEKLY_SUMMARY', layer='long-term')
    conn.executemany("UPDATE posts SET resolved=1 WHERE id=?", [(r['id'],) for r in rows])
    conn.commit()
    conn.close()
    _log(f'周压缩完成：{len(rows)} 条日总结 → 1 条周总结')


def decay_forgotten():
    conn = _db()
    r = conn.execute(
        "UPDATE posts SET importance = importance - 1 WHERE pinned=0 AND importance > 0 "
        "AND created_at < datetime('now','+8 hours','-90 days') "
        "AND (last_recalled_at IS NULL OR last_recalled_at < datetime('now','+8 hours','-90 days'))")
    conn.commit()
    n = r.rowcount
    conn.close()
    if n:
        _log(f'降权 {n} 条（90 天未被想起）')


if __name__ == '__main__':
    retire_stale_memos()
    weekly_compress()
    decay_forgotten()
    _log('cycle done')
