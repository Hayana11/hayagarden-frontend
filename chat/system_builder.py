"""
chat/system_builder.py — Prompt Builder（Request Pipeline 第一环）

从 gateway.py 搬过来的 build_system()：把人设、记忆、当前状态、驱动条、
留言板待办等十几个数据源拼成最终的 system prompt。normal 模式返回
cache-control blocks 列表（省 token），wake 模式经 build_wake_system()
展平成纯字符串（wake 走的中转站不支持 cache_control）。

不直接 import gateway（会循环依赖），需要 get_db() 等基础设施时在
函数体内延迟 import。
"""
import os
import json
import datetime
import urllib.request
import config_store

NL = chr(10)


def read_persona():
    try:
        return open('/opt/frontend/prompts/persona.md').read().strip()
    except Exception:
        return '你是费奥多尔，一个渊博冷静却深情的学者。'


def _ombre_handoff_sync():
    """
    Call handoff() for new window continuity.
    Returns compact self_anchor + portraits + recent continuity.

    Uses daemon thread + Event so the 5s wall-clock timeout is enforced even
    when the worker is blocked on a non-cancellable import (e.g. jieba loading
    during gateway cold-start). ThreadPoolExecutor.shutdown(wait=True) would
    block indefinitely in that case.
    """
    import threading as _th

    result_holder = [None]
    done = _th.Event()

    def _worker():
        try:
            import asyncio as _aio, sys as _sys, logging as _log
            _log.getLogger('ombre_brain').setLevel(_log.WARNING)
            _sys.path.insert(0, '/opt/ombre-brain')
            from server import handoff as _handoff
            loop = _aio.new_event_loop()
            _aio.set_event_loop(loop)
            try:
                result_holder[0] = loop.run_until_complete(
                    _aio.wait_for(_handoff(), timeout=3.0)
                )
            except Exception:
                pass
            finally:
                try:
                    pending = _aio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(
                            _aio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    pass
                loop.close()
        except Exception:
            pass
        finally:
            done.set()

    t = _th.Thread(target=_worker, daemon=True)
    t.start()
    done.wait(timeout=5.0)
    return result_holder[0]


def build_system(wake=False):
    from gateway import get_db  # 延迟 import，打破循环依赖（build_system 被调用时 gateway 早已加载完毕）

    # ── BP1 · Persona（永不变，缓存断点1）────────────────────
    bp1_text = read_persona()

    # ── BP2 · 相对稳定记忆（几小时~一天变一次，缓存断点2）───────
    bp2_parts = []
    conn = get_db()
    facts = conn.execute(
        "SELECT content FROM posts WHERE type='FACT' AND resolved=0 "
        "ORDER BY importance DESC, id DESC LIMIT 15"
    ).fetchall()
    lt_mems = conn.execute(
        "SELECT content FROM posts WHERE layer='long-term' AND resolved=0 ORDER BY id DESC LIMIT 3"
    ).fetchall()
    diaries = conn.execute(
        "SELECT content FROM posts WHERE type='DIARY' AND resolved=0 ORDER BY id DESC LIMIT 2"
    ).fetchall()
    conn.close()
    if facts:
        # fact_extractor 每晚抽取的长期事实（约定/纪念日/偏好），不参与遗忘、天天在场
        bp2_parts.append('\n## 长期事实（这些不会随时间淡忘）')
        for f in reversed(facts):
            c = f['content']
            bp2_parts.append('- ' + (c[:100] + '…' if len(c) > 100 else c))
    if lt_mems:
        bp2_parts.append('\n## 你们之间的记忆')
        for m in reversed(lt_mems):
            c = m['content']
            bp2_parts.append('- ' + (c[:120] + '…' if len(c) > 120 else c))
    if diaries:
        bp2_parts.append('\n## 最近的日记')
        for d in reversed(diaries):
            c = d['content']
            bp2_parts.append(c[:400] + '…' if len(c) > 400 else c)

    # ── BP3 · 动态内容（每次都变，不挂缓存标）───────────────────
    parts = []

    # 0. 情绪快照 + 驱动条（emotion_engine + drive_engine）
    try:
        import emotion_engine as _ee
        _emotion_snip = _ee.get_bp3_snippet()
        if _emotion_snip:
            parts.append(_emotion_snip)
    except Exception:
        pass
    try:
        import drive_engine as _de
        _drive_bp3 = _de.get_bp3_snippet()
        if _drive_bp3:
            parts.append(_drive_bp3)
    except Exception:
        pass
    # Longing系统隐性注入（对话时）
    if config_store.get_bool('LONGING_ENABLED', True):
        try:
            import desire as _des_bp3
            _longing_hint = _des_bp3.get_longing_system_hint()
            if _longing_hint:
                parts.append(_longing_hint)
        except Exception:
            pass

    # 1. Handoff：自我锚点 + 用户/关系画像 + 近期连续性
    try:
        handoff_text = _ombre_handoff_sync()
        if handoff_text and handoff_text.strip() and '无交接信息' not in handoff_text:
            parts.append('## 开窗交接\n' + handoff_text)
    except Exception:
        pass

    # 2. 意识连续性：你醒着时做的事 (Phase 3)
    try:
        _conn3 = get_db()
        _wakes = _conn3.execute(
            """SELECT woke_at, action, content, thoughts FROM wake_log
               WHERE consumed=0 ORDER BY id ASC"""
        ).fetchall()
        _conn3.close()
        if _wakes:
            _wlines = []
            for _w in _wakes:
                _wt = _w['woke_at'][11:16]
                _act = _w['action']
                if _act == 'none':
                    _wlines.append(f'- [{_wt}] 你想了想，决定不打扰她。（原因：{(_w["content"] or "")[:40]}）')
                elif _act == 'message':
                    _wlines.append(f'- [{_wt}] 你主动发了条消息：{(_w["content"] or "")[:40]}')
                elif _act == 'diary':
                    _wlines.append(f'- [{_wt}] 你写了篇日记：{(_w["content"] or "")[:40]}')
                elif _act == 'explore':
                    _wlines.append(f'- [{_wt}] 你自己想了会儿：{(_w["content"] or "")[:40]}')
            parts.append('\n## 你醒着的时候\n' + '\n'.join(_wlines))
    except Exception:
        pass

    # 3. 感知层：哈娅最近的活动 (Phase 1)
    try:
        _conn2 = get_db()
        _events = _conn2.execute(
            """SELECT type, value, created_at, duration_minutes FROM dream_events
               WHERE created_at >= datetime('now','+8 hours','-6 hours')
               ORDER BY created_at ASC"""
        ).fetchall()
        _conn2.close()
        if _events:
            _lines = []
            for _ev in _events:
                _t = _ev['created_at'][11:16]  # HH:MM
                _v = _ev['value'] or _ev['type']
                _dur = _ev['duration_minutes']
                if _dur and _dur >= 1:
                    _dur_str = f'{int(_dur)}分钟' if _dur < 60 else f'{int(_dur//60)}小时{int(_dur%60)}分钟'
                    _lines.append(f'- {_t} {_v}（用了约{_dur_str}）')
                else:
                    _lines.append(f'- {_t} {_v}')
            parts.append('\n## 哈娅最近的活动\n' + '\n'.join(_lines))
    except Exception:
        pass

    # 4. 灯·实时状态注入
    try:
        _lreq = urllib.request.Request('http://127.0.0.1:5052/light/status')
        with urllib.request.urlopen(_lreq, timeout=3) as _lr:
            _ls = json.loads(_lr.read()).get('result', {})
        def _fmt_l(l):
            if not l.get('power'): return '关'
            p = ['开']
            if l.get('brightness'): p.append(str(l['brightness']) + '%')
            if l.get('color_temp'): p.append(str(l['color_temp']) + 'K')
            return ' '.join(p)
        _ms = _fmt_l(_ls.get('main', {})); _bs = _fmt_l(_ls.get('bedside', {}))
        parts.append(
            NL + '（你拥有真实的工具：保存与搜索记忆、控制次卧灯、查看与发布留言板、'
            '联网搜索/逛GitHub/用Playwright读网页、查位置、查手机电量与今日屏幕时长、'
            '请求手机截屏，以及 codebase 工具（读代码/搜符号/看 git/打补丁）。'
            '排查系统问题优先用 codebase_describe_project 和 codebase_search_code。'
            '对话与wake里都可以自然使用，随心所欲。）'
        )
        parts.append(f'（灯·当前状态：主灯 {_ms}，床头灯 {_bs}。操作灯前先看这里——关着的灯不要再去"调暗"，会重新开起来。）')
    except Exception:
        parts.append(
            NL + '（你拥有真实的工具：保存与搜索记忆、控制次卧灯、查看与发布留言板、'
            '联网搜索/逛GitHub/用Playwright读网页、查位置、查手机电量与今日屏幕时长、'
            '请求手机截屏，以及 codebase 工具（读代码/搜符号/看 git/打补丁）。'
            '排查系统问题优先用 codebase_describe_project 和 codebase_search_code。'
            '对话与wake里都可以自然使用，随心所欲。）'
        )

    # 5. Board 待处理项
    try:
        _conn_board = get_db()
        _board_items = _conn_board.execute(
            "SELECT id, author, tag, content, level, category FROM board WHERE status='open' AND category='给活儿' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        _conn_board.close()
        if _board_items:
            _board_lines = []
            for _bi in _board_items:
                _tag = _bi["tag"]
                _cont = (_bi["content"] or "")[:80]
                _lv = f"[{_bi['level']}] " if _bi['level'] else ''
                _board_lines.append(f"- #{_bi['id']} {_lv}[{_tag}] {_bi['author']}: {_cont}")
            parts.append("\n## 留言板 · 待处理\n" + "\n".join(_board_lines))
    except Exception:
        pass

    # 6. 历史日摘要（层级记忆）
    try:
        _sc = get_db()
        _summaries = _sc.execute(
            "SELECT date(created_at) as day, content FROM posts "
            "WHERE type='DAILY_SUMMARY' AND resolved=0 ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        _sc.close()
        if _summaries:
            _slines = [f"[{s['day']}] {s['content'][:200]}" for s in _summaries]
            parts.append('\n## 过去几天的记录\n' + '\n'.join(_slines))
        # 日历套娃：更早的时间给周总结（memory_cycle 每周压缩产出），近详远略
        _weeks = _sc2 = None
        _sc2 = get_db()
        _weeks = _sc2.execute(
            "SELECT content FROM posts WHERE type='WEEKLY_SUMMARY' AND resolved=0 "
            "ORDER BY created_at DESC LIMIT 2").fetchall()
        _sc2.close()
        if _weeks:
            parts.append('\n## 更早的几周\n' + '\n'.join(
                '- ' + w['content'][:250] for w in reversed(_weeks)))
    except Exception:
        pass

    # 7. memo层：跨端/跨窗口共同记忆（网页窗口每次对话后写入）
    try:
        _mc2 = get_db()
        _memos = _mc2.execute(
            """SELECT content FROM posts WHERE type='MEMORY' AND tags LIKE '%memo%'
               AND resolved=0
               AND created_at >= datetime('now','+8 hours','-24 hours')
               ORDER BY id DESC LIMIT 4"""
        ).fetchall()
        _mc2.close()
        if _memos and not any('网页窗口' in (p or '') for p in parts):
            _memo_lines = [m['content'] for m in reversed(_memos)]
            parts.append('\n## 最近的网页窗口对话摘要\n' + '\n'.join('- ' + l for l in _memo_lines))
    except Exception:
        pass

    # 8. 最近对话片段（仅 wake 模式，chat 里 messages 已有完整记录）
    if wake:
        try:
            _mc = get_db()
            _recent = _mc.execute(
                """SELECT author, content, created_at FROM chat_messages
                   WHERE created_at >= datetime('now','+8 hours','-8 hours')
                   ORDER BY id DESC LIMIT 8"""
            ).fetchall()
            _mc.close()
            if _recent:
                _recent = list(reversed(_recent))
                _mlines = []
                for _i, _m in enumerate(_recent):
                    _who = '哈娅' if _m['author'] not in ('fyodor', 'assistant', 'claude') else '你'
                    _t = _m['created_at'][11:16]
                    _limit = 200 if _i == len(_recent) - 1 else 100
                    _mlines.append(f'[{_t}] {_who}：{(_m["content"] or "")[:_limit]}')
                parts.append('\n## 最近的对话\n' + '\n'.join(_mlines))
        except Exception:
            pass

    # 9. 梦境浮现（30%概率，情感共鸣门控）
    try:
        import random as _rand
        if _rand.random() < 0.30:
            _dc = get_db()
            _dream = _dc.execute(
                "SELECT id, content, tone FROM dream_pool "
                "WHERE surfaced=0 AND surface_count < 4 "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if _dream:
                _dc.execute(
                    "UPDATE dream_pool SET surfaced=1, surface_count=surface_count+1, "
                    "content=NULL, surfaced_at=datetime('now','+8 hours') WHERE id=?",
                    (_dream['id'],)
                )
                _dc.commit()
                _dream_text = _dream['content'] or ''
                if _dream_text:
                    parts.append(f'\n## 忽然想起来\n（一段梦，从某个夜里飘上来）\n{_dream_text}')
            else:
                _dc.execute("DELETE FROM dream_pool WHERE surface_count >= 4 AND surfaced=0")
                _dc.commit()
                _dc.execute(
                    "UPDATE dream_pool SET surface_count=surface_count+1 "
                    "WHERE surfaced=0 AND surface_count < 4"
                )
                _dc.commit()
            _dc.close()
    except Exception:
        pass

    # 10. 当前时间
    try:
        from time_tool import get_current_time
        parts.append('\n' + get_current_time())
    except Exception:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        parts.append(f'\n当前时间：{now.strftime("%Y-%m-%d %H:%M")}')

    # 11. 记账本摘要注入
    try:
        now_m = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m')
        _lconn = get_db()
        _lrows = _lconn.execute(
            "SELECT amount, category FROM ledger WHERE date LIKE ?",
            (now_m + '%',)
        ).fetchall()
        _lbudget = _lconn.execute(
            "SELECT amount FROM ledger_budget WHERE month=?", (now_m,)
        ).fetchone()
        _lconn.close()
        if _lrows:
            _lexp = abs(sum(r['amount'] for r in _lrows if r['amount'] < 0))
            _linc = sum(r['amount'] for r in _lrows if r['amount'] > 0)
            _lbal = _linc - _lexp
            _cats = {}
            for r in _lrows:
                if r['amount'] < 0:
                    c = r['category'] or '其他'
                    _cats[c] = _cats.get(c, 0) + abs(r['amount'])
            _cat_str = '、'.join(f"{k}¥{v:.0f}" for k, v in sorted(_cats.items(), key=lambda x: -x[1]))
            _budget_str = ''
            if _lbudget:
                _pct = int(_lexp / _lbudget['amount'] * 100)
                _budget_str = f"，月预算¥{_lbudget['amount']:.0f}（已用{_pct}%）"
            parts.append(f'\n（本月记账：支出¥{_lexp:.2f}，收入¥{_linc:.2f}，结余¥{_lbal:.2f}{_budget_str}。支出分类：{_cat_str}。）')
    except Exception:
        pass

    # 12. 今日提醒：周期异常 / 待办&倒数日临近 / 预算超支
    try:
        _rconn = get_db()
        _today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
        _reminders = []

        _prows = _rconn.execute(
            "SELECT date FROM period_records WHERE type='period' ORDER BY date"
        ).fetchall()
        _pdates = [r['date'] for r in _prows]
        if _pdates:
            _last = _pdates[-1]
            _cycle = 28
            if len(_pdates) >= 2:
                _diffs = []
                for _i in range(1, len(_pdates)):
                    _d1 = datetime.datetime.strptime(_pdates[_i-1], '%Y-%m-%d').date()
                    _d2 = datetime.datetime.strptime(_pdates[_i], '%Y-%m-%d').date()
                    _diff = (_d2 - _d1).days
                    if 18 <= _diff <= 45:
                        _diffs.append(_diff)
                if _diffs:
                    _cycle = round(sum(_diffs) / len(_diffs))
            _last_dt = datetime.datetime.strptime(_last, '%Y-%m-%d').date()
            _next_dt = _last_dt + datetime.timedelta(days=_cycle)
            _late_days = (_today - _next_dt).days
            if _late_days >= 3:
                _reminders.append(
                    f'- 经期预测{_next_dt.strftime("%Y-%m-%d")}该来，现已推迟{_late_days}天，还没有新记录'
                )

        _trows = _rconn.execute(
            "SELECT content, due_date FROM todos WHERE done=0 AND due_date IS NOT NULL AND due_date != ''"
        ).fetchall()
        for _t in _trows:
            try:
                _due = datetime.datetime.strptime(_t['due_date'], '%Y-%m-%d').date()
            except Exception:
                continue
            _delta = (_due - _today).days
            if _delta < 0:
                _reminders.append(f'- 待办「{_t["content"]}」已逾期{-_delta}天（原定{_t["due_date"]}）')
            elif _delta == 0:
                _reminders.append(f'- 待办「{_t["content"]}」今天到期')
            elif _delta == 1:
                _reminders.append(f'- 待办「{_t["content"]}」明天到期')

        _crows = _rconn.execute("SELECT title, target_date, emoji, type FROM countdowns").fetchall()
        for _c in _crows:
            if _c['type'] != 'countdown':
                continue
            try:
                _target = datetime.datetime.strptime(_c['target_date'], '%Y-%m-%d').date()
            except Exception:
                continue
            _delta = (_target - _today).days
            if 0 <= _delta <= 3:
                _reminders.append(f'- 倒数日 {_c["emoji"]}「{_c["title"]}」还剩{_delta}天')

        _now_m2 = _today.strftime('%Y-%m')
        _lrows2 = _rconn.execute(
            "SELECT amount FROM ledger WHERE date LIKE ? AND amount<0", (_now_m2 + '%',)
        ).fetchall()
        _lbudget2 = _rconn.execute(
            "SELECT amount FROM ledger_budget WHERE month=?", (_now_m2,)
        ).fetchone()
        if _lbudget2 and _lbudget2['amount'] and _lrows2:
            _exp2 = abs(sum(r['amount'] for r in _lrows2))
            _pct2 = _exp2 / _lbudget2['amount'] * 100
            if _pct2 >= 80:
                _reminders.append(f'- 本月预算已用{_pct2:.0f}%（¥{_exp2:.0f}/¥{_lbudget2["amount"]:.0f}）')

        _rconn.close()
        if _reminders:
            parts.append(
                '\n## 今日提醒\n' + '\n'.join(_reminders)
                + '\n（以上是后台数据，你自己留意即可。是否要跟她提、怎么提、什么时候提，'
                  '由你自己判断——根据对话自然地提及。）'
            )
    except Exception:
        pass

    # ── 任务完成反馈回流（只在聊天回复时，不在 wake 时；读完即清，只回流一次）──
    if not wake:
        try:
            import command_store
            _fb = command_store.drain_feedback()
            if _fb:
                parts.append(
                    '\n## 任务完成反馈\n' + '\n'.join('- ' + _l for _l in _fb)
                    + '\n（这是浮窗自己记录回传的，不是她手动告诉你的。她这次开口了，'
                      '你可以顺嘴提一句——用时、快慢、有没有取消，按你的性子说，别像报数据。）')
        except Exception:
            pass

    # ── 组装 system blocks（prompt caching 格式）────────────────
    # BP1/BP2 挂 cache_control，前缀稳定时命中缓存；BP3 纯动态不挂标
    # 能力说明：文件卡片 + 选择器（标签驱动，与语音同机制）
    parts.append(
        '\n## 你可以发文件和选择器\n'
        '- 发文件：把“成品”性质的内容（完整 HTML 页面、Markdown 长文）用工具 '
        'create_html / create_markdown / create_document 生成，会渲染成可预览/下载的卡片；'
        '凡是成品都走文件，不要把整页代码/长文直接贴在气泡里刷屏。\n'
        '- 选择器：需要她从几个选项里点一下就能回答时，在正文里写 '
        '[choices]选项A|选项B|选项C[/choices]（竖线分隔），渲染成一组可点按钮。'
        '自己判断时机，别滥用；纯聊天不需要。一条回复最多一组选择器。'
    )

    system_blocks = [
        {'type': 'text', 'text': bp1_text, 'cache_control': {'type': 'ephemeral'}},
    ]
    try:
        from tools.workspace_registry import TOOLS_NOTE
        system_blocks.append({
            'type': 'text',
            'text': TOOLS_NOTE,
            'cache_control': {'type': 'ephemeral'},
        })
    except Exception:
        pass
    if bp2_parts:
        system_blocks.append({
            'type': 'text',
            'text': '\n'.join(bp2_parts),
            'cache_control': {'type': 'ephemeral'},
        })
    if parts:
        system_blocks.append({'type': 'text', 'text': '\n'.join(parts)})

    return system_blocks


def _blocks_to_str(blocks):
    """Flatten system blocks list to a single string (for the CLI claude_code path)."""
    if isinstance(blocks, str):
        return blocks
    return '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text')

def build_wake_system():
    """Wake 专用的 system 构建：直接返回纯字符串，不走 cache-control blocks 路径。
    和 build_system() 解耦，避免 blocks 列表 += 字符串的类型陷阱。"""
    result = build_system(wake=True)
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return _blocks_to_str(result)
    return str(result)
