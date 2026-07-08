#!/usr/bin/env python3.11
"""长期事实抽取（记忆升级·方案三）——每晚 05:40 cron，赶在 summarizer(06:00) 之前。

问题：约定、纪念日、偏好这类"事实"以前只活在某天的日摘要里，套娃压缩两层后
细节就磨没了。事件可以衰减，事实不该衰减——episodic / semantic 分离。

做法：读前一天的 chat_messages，让 DeepSeek 抽取"值得永久记住的稳定事实"
（约定/纪念日/偏好/身份信息/习惯/重要物品），与已有 FACT 去重后存入
posts(type='FACT', layer='long-term')。memory_cycle 的降权和周压缩不碰 long-term/core；
system_builder 把 FACT 注入 BP2，天天在场。

用法：fact_extractor.py [--dry-run] [--date YYYY-MM-DD]
"""
import datetime
import sqlite3
import sys

sys.path.insert(0, '/opt/frontend/tools')
sys.path.insert(0, '/opt/frontend')
import llm_lite
import memory_tool
from memory_tool import is_near_duplicate, normalize_content

DB = '/opt/frontend/memories.db'
MAX_FACTS_PER_DAY = 6


def _log(msg):
    ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    print('[%s] %s' % (ts, msg), flush=True)


def _db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _day_dialogue(day):
    conn = _db()
    rows = conn.execute(
        "SELECT author, content FROM chat_messages WHERE date(created_at)=? ORDER BY id ASC",
        (day,)).fetchall()
    conn.close()
    lines = []
    for r in rows:
        c = (r['content'] or '').strip()
        if not c:
            continue
        who = '哈娅' if r['author'] == 'hayana' else '费奥多尔'
        lines.append(who + ': ' + c[:150])
    return lines


def _existing_facts():
    conn = _db()
    rows = conn.execute(
        "SELECT content FROM posts WHERE type='FACT' ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return [r['content'] for r in rows]


def extract(day, dry_run=False):
    lines = _day_dialogue(day)
    if not lines:
        _log('no messages on %s' % day)
        return 0
    # 全量分块喂——事实抽取不能采样，采掉的那句可能正是约定本身
    chunks, cur, cur_len = [], [], 0
    for ln in lines:
        cur.append(ln)
        cur_len += len(ln)
        if cur_len > 5000:
            chunks.append('\n'.join(cur))
            cur, cur_len = [], 0
    if cur:
        chunks.append('\n'.join(cur))

    known = _existing_facts()
    known_block = '\n'.join('- ' + k[:80] for k in known[:60]) or '（暂无）'

    facts = []
    for i, chunk in enumerate(chunks):
        prompt = (
            '下面是费奥多尔和哈娅 %s 的一段对话记录。请抽取其中"值得永久记住的稳定事实"。\n'
            '只要以下几类：\n'
            '- 约定/承诺（谁答应了什么、计划做什么）\n'
            '- 纪念日/重要日期\n'
            '- 稳定偏好（喜欢/讨厌什么，长期成立的）\n'
            '- 身份/环境信息（住址变化、新买的重要物品、工作变动等）\n'
            '- 长期习惯\n'
            '不要：当天的情绪、一次性的琐事、闲聊内容、对话过程本身。\n'
            '每条事实一句话、自包含（单独读也能懂，含必要的时间/人名），15-60 字。\n'
            '以下事实已经记住了，语义重复的不要再输出：\n%s\n\n'
            '只返回 JSON 数组（每项一个字符串）；没有新事实就返回 []。\n\n'
            '对话记录：\n%s'
        ) % (day, known_block, chunk)
        got = llm_lite.ask(prompt, max_tokens=500, expect_json=True)
        if isinstance(got, list):
            facts.extend(str(f).strip() for f in got if str(f).strip())
        else:
            _log('chunk %d/%d LLM failed' % (i + 1, len(chunks)))

    # 去重（与库内 + 本批内），限量防洪
    saved = 0
    for f in facts:
        if len(f) < 8:
            continue
        fn = normalize_content(f)
        if any(is_near_duplicate(fn, normalize_content(k)) for k in known):
            continue
        known.append(f)
        _log('FACT: %s' % f)
        if not dry_run:
            memory_tool.save_memory(f, type='FACT', layer='long-term',
                                    tags='fact,auto', importance=5)
        saved += 1
        if saved >= MAX_FACTS_PER_DAY:
            break
    _log('day %s: %d new facts%s' % (day, saved, ' (dry-run)' if dry_run else ''))
    return saved


if __name__ == '__main__':
    _dry = '--dry-run' in sys.argv
    if '--date' in sys.argv:
        _day = sys.argv[sys.argv.index('--date') + 1]
    else:
        _day = ((datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
                - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    extract(_day, dry_run=_dry)
