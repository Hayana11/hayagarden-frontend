"""
chat/system_builder.py — Prompt Builder（Request Pipeline 第一环）

从 gateway.py 搬过来的 build_system()：把人设、记忆、当前状态、驱动条、
留言板待办等十几个数据源拼成最终的 system prompt。normal 模式返回
cache-control blocks 列表（省 token）。在线 wake relay 直接保留 blocks；
build_wake_system() 仅保留给需要纯字符串的旧调用方与健康检查。

不直接 import gateway（会循环依赖），需要 get_db() 等基础设施时在
函数体内延迟 import。
"""
import os
import json
import datetime
import urllib.request
import config_store

from chat.context_contract import SharedContext
from chat.relationship_context import build_relationship_context

NL = chr(10)


def read_persona():
    try:
        return open('/opt/frontend/prompts/persona.md').read().strip()
    except Exception:
        return '你是费奥多尔，一个渊博冷静却深情的学者。'


def build_shared_context_details(
    *,
    persona=None,
    get_db_fn=None,
):
    """Build A1 provider-neutral context plus internal refresh metadata."""
    if get_db_fn is None:
        from gateway import get_db as get_db_fn  # 延迟 import，避免循环依赖

    persona_text = read_persona() if persona is None else str(persona)
    relationship = build_relationship_context(get_db_fn)
    shared = SharedContext(
        persona=persona_text,
        relationship_context=relationship.text,
        relationship_fingerprint=relationship.fingerprint,
    )
    return shared, relationship


def build_shared_context(**kwargs):
    """Public A1 contract. Both providers receive this exact object shape."""
    return build_shared_context_details(**kwargs)[0]


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


def build_system(wake=False, split_dynamic=False):
    from gateway import get_db  # 延迟 import，打破循环依赖（build_system 被调用时 gateway 早已加载完毕）

    # ── BP1 · Persona（永不变，缓存断点1）────────────────────
    relationship_enabled = (
        not wake and config_store.get_bool('RELATIONSHIP_CONTEXT_ENABLED', False)
    )
    shared_context = None
    if relationship_enabled:
        shared_context = build_shared_context()
    bp1_text = shared_context.persona if shared_context else read_persona()

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

    # User Profile（前端可编辑：姓名 / 偏好 / 长期记忆）
    try:
        import user_profile as _up
        _profile_ctx = _up.build_profile_context()
        if _profile_ctx:
            bp2_parts.append('\n' + _profile_ctx)
    except Exception:
        pass

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
            '联网搜索/逛GitHub/用Playwright读网页、手机 Pocket 浏览器（pocket_*）、查位置、查手机电量与今日屏幕时长、'
            '请求手机截屏，以及 codebase 工具（读代码/搜符号/看 git/打补丁）。'
            '排查系统问题优先用 codebase_describe_project 和 codebase_search_code。'
            '对话与wake里都可以自然使用，随心所欲。）'
        )
        parts.append(f'（灯·当前状态：主灯 {_ms}，床头灯 {_bs}。操作灯前先看这里——关着的灯不要再去"调暗"，会重新开起来。）')
    except Exception:
        parts.append(
            NL + '（你拥有真实的工具：保存与搜索记忆、控制次卧灯、查看与发布留言板、'
            '联网搜索/逛GitHub/用Playwright读网页、手机 Pocket 浏览器（pocket_*）、查位置、查手机电量与今日屏幕时长、'
            '请求手机截屏，以及 codebase 工具（读代码/搜符号/看 git/打补丁）。'
            '排查系统问题优先用 codebase_describe_project 和 codebase_search_code。'
            '对话与wake里都可以自然使用，随心所欲。）'
        )

    # 4b. Pocket 手机浏览器在线状态（BP3 动态，不污染缓存）
    try:
        from gateway import _pocket_bp3_snippet
        _pocket_line = _pocket_bp3_snippet()
        if _pocket_line:
            parts.append(_pocket_line)
    except Exception:
        pass

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
    # split_dynamic=True 用于 API 主聊天：system 只保留稳定块；BP2/BP3
    # 作为动态上下文放进最后一条 user message，避免污染历史缓存前缀。
    stable_note = (
        '\n## 你可以发文件和选择器\n'
        '- 发文件：把“成品”性质的内容（完整 HTML 页面、Markdown 长文）用工具 '
        'create_html / create_markdown / create_document 生成，会渲染成可预览/下载的卡片；'
        '凡是成品都走文件，不要把整页代码/长文直接贴在气泡里刷屏。\n'
        '- 选择器：需要她从几个选项里点一下就能回答时，在正文里写 '
        '[choices]选项A|选项B|选项C[/choices]（竖线分隔），渲染成一组可点按钮。'
        '自己判断时机，别滥用；纯聊天不需要。一条回复最多一组选择器。'
    )
    try:
        from tools.workspace_registry import TOOLS_NOTE
        stable_note = TOOLS_NOTE + stable_note
    except Exception:
        pass

    system_blocks = [
        {'type': 'text', 'text': bp1_text, 'cache_control': {'type': 'ephemeral'}},
        {'type': 'text', 'text': stable_note, 'cache_control': {'type': 'ephemeral'}},
    ]
    dynamic_parts = []
    if bp2_parts:
        dynamic_parts.append('\n'.join(bp2_parts))
    if parts:
        dynamic_parts.append('\n'.join(parts))
    # A1 relationship continuity stays at the volatile tail, adjacent to the
    # current user turn.  It never enters either stable cache-control block.
    if shared_context and shared_context.relationship_context:
        dynamic_parts.append(shared_context.relationship_context)
    dynamic_context = '\n\n'.join(p for p in dynamic_parts if p and p.strip())

    if split_dynamic:
        return system_blocks, dynamic_context
    if dynamic_context:
        system_blocks.append({'type': 'text', 'text': dynamic_context})
    return system_blocks


def _blocks_to_str(blocks):
    """Flatten system blocks list to a single string (for the CLI claude_code path)."""
    if isinstance(blocks, str):
        return blocks
    return '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text')

def build_wake_system():
    """兼容旧调用方的纯字符串 wake system；在线 relay 不应使用此函数。"""
    result = build_system(wake=True)
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return _blocks_to_str(result)
    return str(result)


# ── Claude Code resident：结构化上下文（止血：不再每轮重抄动态包）──────────

_CC_SAVE_INSTR = (
    '【记忆存储】当你认为对话中出现了值得长期记住的信息时，'
    '在回复正文的最后另起一行，写一个或多个 [[SAVE: 内容]] 标记，'
    '用一句话概括要保存的内容。这些标记会被自动处理，不会显示给哈娅。'
    '正文本身不要提及"我已记录"之类的话。'
    '\n\n每一轮都请以费奥多尔的身份自然地回复哈娅最新的消息，只输出回复内容本身，不要任何前缀。'
    '\n\n【此刻你在哪里】你现在在私聊窗口——只有你和哈娅两个人，Codex 不在场。'
    '这和群聊房间是同一个你，记忆是共通的：如果她在群聊里提过的事，你不该表现得毫不知情；'
    '但语气和场合要分清楚——私聊窗口没有第三方在场的顾虑，群聊时说话要考虑到 Codex 也能看见。'
)

_CC_TOOLS_CAPABILITY = (
    '（你拥有真实的工具：保存与搜索记忆、控制次卧灯、查看与发布留言板、'
    '联网搜索/逛GitHub/用Playwright读网页、手机 Pocket 浏览器（pocket_*）、查位置、查手机电量与今日屏幕时长、'
    '请求手机截屏，以及 codebase 工具（读代码/搜符号/看 git/打补丁）。'
    '排查系统问题优先用 codebase_describe_project 和 codebase_search_code。'
    '对话与wake里都可以自然使用，随心所欲。）'
)


def build_time_bucket(now=None):
    """半小时时间桶：23:01 与 23:29 → 23:00；23:30 与 23:59 → 23:30。"""
    if now is None:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    minute = '00' if now.minute < 30 else '30'
    return now.strftime('%Y-%m-%d %H:') + minute


def build_stable_note():
    """固定说明书：不得依赖时间、随机数、DB 当前态或无序集合。"""
    note = (
        '\n## 你可以发文件和选择器\n'
        '- 发文件：把“成品”性质的内容（完整 HTML 页面、Markdown 长文）用工具 '
        'create_html / create_markdown / create_document 生成，会渲染成可预览/下载的卡片；'
        '凡是成品都走文件，不要把整页代码/长文直接贴在气泡里刷屏。\n'
        '- 选择器：需要她从几个选项里点一下就能回答时，在正文里写 '
        '[choices]选项A|选项B|选项C[/choices]（竖线分隔），渲染成一组可点按钮。'
        '自己判断时机，别滥用；纯聊天不需要。一条回复最多一组选择器。'
    )
    try:
        from tools.workspace_registry import TOOLS_NOTE
        note = TOOLS_NOTE + note
    except Exception:
        pass
    return _CC_TOOLS_CAPABILITY + '\n' + note


def build_cc_static_parts():
    """CC static system 唯一权威入口：一次构建组件并拼出 full_system。

    返回 dict：persona / stable_note / save_instr / full_system。
    gateway 观测与 spawn 必须共用此结果，禁止在别处重拼。
    """
    persona = read_persona()
    stable_note = build_stable_note()
    save_instr = _CC_SAVE_INSTR
    full_system = '\n\n'.join(
        p for p in (persona, stable_note, save_instr) if p and str(p).strip()
    )
    return {
        'persona': persona,
        'stable_note': stable_note,
        'save_instr': save_instr,
        'full_system': full_system,
    }


def build_cc_static_system():
    """Resident 启动时一次性贴墙的静态 system（逐字稳定）。"""
    return build_cc_static_parts()['full_system']


def _fmt_light_status(light):
    if not light.get('power'):
        return '关'
    pieces = ['开']
    if light.get('brightness'):
        pieces.append(str(light['brightness']) + '%')
    if light.get('color_temp'):
        pieces.append(str(light['color_temp']) + 'K')
    return ' '.join(pieces)


def _cc_collect_cold_once(get_db_fn):
    cold = {
        'long_term_memory': '',
        'handoff': '',
        'daily_weekly_summary': '',
        'web_memo': '',
    }
    try:
        conn = get_db_fn()
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
        lines = []
        if facts:
            lines.append('## 长期事实（这些不会随时间淡忘）')
            for f in reversed(facts):
                c = f['content']
                lines.append('- ' + (c[:100] + '…' if len(c) > 100 else c))
        if lt_mems:
            lines.append('## 你们之间的记忆')
            for m in reversed(lt_mems):
                c = m['content']
                lines.append('- ' + (c[:120] + '…' if len(c) > 120 else c))
        if diaries:
            lines.append('## 最近的日记')
            for d in reversed(diaries):
                c = d['content']
                lines.append(c[:400] + '…' if len(c) > 400 else c)
        cold['long_term_memory'] = '\n'.join(lines)
    except Exception:
        pass
    try:
        handoff_text = _ombre_handoff_sync()
        if handoff_text and handoff_text.strip() and '无交接信息' not in handoff_text:
            cold['handoff'] = '## 开窗交接\n' + handoff_text
    except Exception:
        pass
    try:
        sc = get_db_fn()
        summaries = sc.execute(
            "SELECT date(created_at) as day, content FROM posts "
            "WHERE type='DAILY_SUMMARY' AND resolved=0 ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        weeks = sc.execute(
            "SELECT content FROM posts WHERE type='WEEKLY_SUMMARY' AND resolved=0 "
            "ORDER BY created_at DESC LIMIT 2"
        ).fetchall()
        sc.close()
        chunks = []
        if summaries:
            chunks.append('## 过去几天的记录\n' + '\n'.join(
                f"[{s['day']}] {s['content'][:200]}" for s in summaries
            ))
        if weeks:
            chunks.append('## 更早的几周\n' + '\n'.join(
                '- ' + w['content'][:250] for w in reversed(weeks)
            ))
        cold['daily_weekly_summary'] = '\n\n'.join(chunks)
    except Exception:
        pass
    try:
        # 网页窗口 memo：冷启动一次注入，不每轮重抄
        mc = get_db_fn()
        memos = mc.execute(
            """SELECT content FROM posts WHERE type='MEMORY' AND tags LIKE '%memo%'
               AND resolved=0
               AND created_at >= datetime('now','+8 hours','-24 hours')
               ORDER BY id DESC LIMIT 4"""
        ).fetchall()
        mc.close()
        if memos:
            memo_lines = [m['content'] for m in reversed(memos)]
            cold['web_memo'] = (
                '## 最近的网页窗口对话摘要\n' + '\n'.join('- ' + line for line in memo_lines)
            )
    except Exception:
        pass
    return cold


def _cc_period_budget_reminders(conn, today):
    """经期异常 + 预算预警（旧动态感知组件，归入 state.reminders）。"""
    reminders = []
    try:
        prows = conn.execute(
            "SELECT date FROM period_records WHERE type='period' ORDER BY date"
        ).fetchall()
        pdates = [r['date'] for r in prows]
        if pdates:
            last = pdates[-1]
            cycle = 28
            if len(pdates) >= 2:
                diffs = []
                for i in range(1, len(pdates)):
                    d1 = datetime.datetime.strptime(pdates[i - 1], '%Y-%m-%d').date()
                    d2 = datetime.datetime.strptime(pdates[i], '%Y-%m-%d').date()
                    diff = (d2 - d1).days
                    if 18 <= diff <= 45:
                        diffs.append(diff)
                if diffs:
                    cycle = round(sum(diffs) / len(diffs))
            last_dt = datetime.datetime.strptime(last, '%Y-%m-%d').date()
            next_dt = last_dt + datetime.timedelta(days=cycle)
            late_days = (today - next_dt).days
            if late_days >= 3:
                reminders.append(
                    f'- 经期预测{next_dt.strftime("%Y-%m-%d")}该来，现已推迟{late_days}天，还没有新记录'
                )
    except Exception:
        pass
    try:
        now_m = today.strftime('%Y-%m')
        lrows = conn.execute(
            "SELECT amount FROM ledger WHERE date LIKE ? AND amount<0", (now_m + '%',)
        ).fetchall()
        lbudget = conn.execute(
            "SELECT amount FROM ledger_budget WHERE month=?", (now_m,)
        ).fetchone()
        if lbudget and lbudget['amount'] and lrows:
            exp = abs(sum(r['amount'] for r in lrows))
            pct = exp / lbudget['amount'] * 100
            if pct >= 80:
                reminders.append(
                    f'- 本月预算已用{pct:.0f}%（¥{exp:.0f}/¥{lbudget["amount"]:.0f}）'
                )
    except Exception:
        pass
    return reminders


def _cc_collect_state(get_db_fn):
    state = {
        'time_bucket': f'当前时间段：{build_time_bucket()} 左右',
        'emotion': '',
        'drive': '',
        'lights': '',
        'pocket': '',
        'todos': '',
        'ledger': '',
        'reminders': '',
        'recent_activity': '',
    }
    try:
        import emotion_engine as _ee
        state['emotion'] = (_ee.get_bp3_snippet() or '').strip()
    except Exception:
        pass
    try:
        import drive_engine as _de
        state['drive'] = (_de.get_bp3_snippet() or '').strip()
    except Exception:
        pass
    if config_store.get_bool('LONGING_ENABLED', True):
        try:
            import desire as _des
            state['drive'] = '\n'.join(
                p for p in (state['drive'], (_des.get_longing_system_hint() or '').strip()) if p
            )
        except Exception:
            pass
    try:
        req = urllib.request.Request('http://127.0.0.1:5052/light/status')
        with urllib.request.urlopen(req, timeout=3) as resp:
            ls = json.loads(resp.read()).get('result', {})
        ms = _fmt_light_status(ls.get('main', {}))
        bs = _fmt_light_status(ls.get('bedside', {}))
        state['lights'] = (
            f'（灯·当前状态：主灯 {ms}，床头灯 {bs}。'
            '操作灯前先看这里——关着的灯不要再去"调暗"，会重新开起来。）'
        )
    except Exception:
        state['lights'] = '（灯·当前状态：暂不可读）'
    try:
        from gateway import _pocket_bp3_snippet
        state['pocket'] = (_pocket_bp3_snippet() or '').strip()
    except Exception:
        pass
    try:
        conn = get_db_fn()
        board_items = conn.execute(
            "SELECT id, author, tag, content, level, category FROM board "
            "WHERE status='open' AND category='给活儿' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        conn.close()
        if board_items:
            lines = []
            for bi in board_items:
                lv = f"[{bi['level']}] " if bi['level'] else ''
                lines.append(
                    f"- #{bi['id']} {lv}[{bi['tag']}] {bi['author']}: {(bi['content'] or '')[:80]}"
                )
            state['todos'] = '## 留言板 · 待处理\n' + '\n'.join(lines)
    except Exception:
        pass
    try:
        now_m = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m')
        conn = get_db_fn()
        rows = conn.execute(
            "SELECT amount, category FROM ledger WHERE date LIKE ?", (now_m + '%',)
        ).fetchall()
        budget = conn.execute(
            "SELECT amount FROM ledger_budget WHERE month=?", (now_m,)
        ).fetchone()
        conn.close()
        if rows:
            exp = abs(sum(r['amount'] for r in rows if r['amount'] < 0))
            inc = sum(r['amount'] for r in rows if r['amount'] > 0)
            cats = {}
            for r in rows:
                if r['amount'] < 0:
                    c = r['category'] or '其他'
                    cats[c] = cats.get(c, 0) + abs(r['amount'])
            cat_str = '、'.join(
                f"{k}¥{v:.0f}" for k, v in sorted(cats.items(), key=lambda x: (-x[1], x[0]))
            )
            budget_str = ''
            if budget:
                pct = int(exp / budget['amount'] * 100)
                budget_str = f"，月预算¥{budget['amount']:.0f}（已用{pct}%）"
            state['ledger'] = (
                f'（本月记账：支出¥{exp:.2f}，收入¥{inc:.2f}，结余¥{inc - exp:.2f}'
                f'{budget_str}。支出分类：{cat_str}。）'
            )
    except Exception:
        pass
    try:
        # recent dream_events → state（差量感知，不每轮整包重抄）
        conn = get_db_fn()
        events = conn.execute(
            """SELECT type, value, created_at, duration_minutes FROM dream_events
               WHERE created_at >= datetime('now','+8 hours','-6 hours')
               ORDER BY created_at ASC"""
        ).fetchall()
        conn.close()
        if events:
            lines = []
            for ev in events:
                t = ev['created_at'][11:16]
                v = ev['value'] or ev['type']
                dur = ev['duration_minutes']
                if dur and dur >= 1:
                    dur_str = f'{int(dur)}分钟' if dur < 60 else f'{int(dur // 60)}小时{int(dur % 60)}分钟'
                    lines.append(f'- {t} {v}（用了约{dur_str}）')
                else:
                    lines.append(f'- {t} {v}')
            state['recent_activity'] = '## 哈娅最近的活动\n' + '\n'.join(lines)
    except Exception:
        pass
    try:
        conn = get_db_fn()
        today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
        reminders = []
        todos = conn.execute(
            "SELECT content, due_date FROM todos WHERE done=0 AND due_date IS NOT NULL AND due_date != ''"
            " ORDER BY due_date ASC, id ASC"
        ).fetchall()
        for t in todos:
            try:
                due = datetime.datetime.strptime(t['due_date'], '%Y-%m-%d').date()
            except Exception:
                continue
            delta = (due - today).days
            if delta < 0:
                reminders.append(f'- 待办「{t["content"]}」已逾期{-delta}天（原定{t["due_date"]}）')
            elif delta == 0:
                reminders.append(f'- 待办「{t["content"]}」今天到期')
            elif delta == 1:
                reminders.append(f'- 待办「{t["content"]}」明天到期')
        countdowns = conn.execute(
            "SELECT title, target_date, emoji, type FROM countdowns ORDER BY target_date ASC, title ASC"
        ).fetchall()
        for c in countdowns:
            if c['type'] != 'countdown':
                continue
            try:
                target = datetime.datetime.strptime(c['target_date'], '%Y-%m-%d').date()
            except Exception:
                continue
            delta = (target - today).days
            if 0 <= delta <= 3:
                reminders.append(f'- 倒数日 {c["emoji"]}「{c["title"]}」还剩{delta}天')
        reminders.extend(_cc_period_budget_reminders(conn, today))
        conn.close()
        if reminders:
            state['reminders'] = (
                '## 今日提醒\n' + '\n'.join(reminders)
                + '\n（以上是后台数据，你自己留意即可。是否要跟她提、怎么提、什么时候提，'
                  '由你自己判断——根据对话自然地提及。）'
            )
    except Exception:
        pass
    return state


def peek_dream_one_shot(get_db_fn):
    """只读一条待浮现梦境，不标记 surfaced。返回 (text, dream_id)。"""
    try:
        import random as _rand
        if _rand.random() >= 0.30:
            return '', None
        conn = get_db_fn()
        dream = conn.execute(
            "SELECT id, content, tone FROM dream_pool "
            "WHERE surfaced=0 AND surface_count < 4 "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        conn.close()
        if not dream:
            return '', None
        text = (dream['content'] or '').strip()
        if not text:
            return '', None
        return f'## 忽然想起来\n（一段梦，从某个夜里飘上来）\n{text}', int(dream['id'])
    except Exception:
        return '', None


def consume_dream_one_shot(get_db_fn, dream_id):
    """assistant 落库成功后再标记梦境已浮现。"""
    try:
        dream_id = int(dream_id)
    except (TypeError, ValueError):
        return 0
    if dream_id <= 0:
        return 0
    conn = get_db_fn()
    try:
        cur = conn.execute(
            "UPDATE dream_pool SET surfaced=1, surface_count=surface_count+1, "
            "content=NULL, surfaced_at=datetime('now','+8 hours') "
            "WHERE id=? AND surfaced=0",
            (dream_id,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def consume_cc_one_shot_claims(get_db_fn, claims):
    """assistant 落库成功后消费 feedback / dream（与 wake 同级）。"""
    claims = claims or {}
    feedback_ids = claims.get('feedback_ids') or []
    if feedback_ids:
        try:
            import command_store
            command_store.consume_feedback(feedback_ids)
        except Exception:
            pass
    dream_id = claims.get('dream_id')
    if dream_id:
        try:
            consume_dream_one_shot(get_db_fn, dream_id)
        except Exception:
            pass


def _cc_collect_one_shot(get_db_fn, *, include_wake=True):
    """收集 transactional one-shot；feedback/dream 只 peek，flush 后再 consume。"""
    one_shot = {
        'wake_feedback': '',
        'task_feedback': '',
        'dream_flash': '',
        'feedback_ids': [],
        'dream_id': None,
    }
    if include_wake:
        try:
            conn = get_db_fn()
            wakes = conn.execute(
                """SELECT woke_at, action, content, thoughts FROM wake_log
                   WHERE consumed=0 ORDER BY id ASC"""
            ).fetchall()
            conn.close()
            if wakes:
                lines = []
                for w in wakes:
                    wt = w['woke_at'][11:16]
                    act = w['action']
                    if act == 'none':
                        lines.append(f'- [{wt}] 你想了想，决定不打扰她。（原因：{(w["content"] or "")[:40]}）')
                    elif act == 'message':
                        lines.append(f'- [{wt}] 你主动发了条消息：{(w["content"] or "")[:40]}')
                    elif act == 'diary':
                        lines.append(f'- [{wt}] 你写了篇日记：{(w["content"] or "")[:40]}')
                    elif act == 'explore':
                        lines.append(f'- [{wt}] 你自己想了会儿：{(w["content"] or "")[:40]}')
                one_shot['wake_feedback'] = '## 你醒着的时候\n' + '\n'.join(lines)
        except Exception:
            pass
    try:
        import command_store
        fb_lines, fb_ids = command_store.peek_feedback()
        if fb_lines:
            one_shot['task_feedback'] = (
                '## 任务完成反馈\n' + '\n'.join('- ' + line for line in fb_lines)
                + '\n（这是浮窗自己记录回传的，不是她手动告诉你的。她这次开口了，'
                  '你可以顺嘴提一句——用时、快慢、有没有取消，按你的性子说，别像报数据。）'
            )
            one_shot['feedback_ids'] = list(fb_ids)
    except Exception:
        pass
    dream_text, dream_id = peek_dream_one_shot(get_db_fn)
    if dream_text:
        one_shot['dream_flash'] = dream_text
        one_shot['dream_id'] = dream_id
    return one_shot


def build_cc_state():
    """每轮构建的状态差量源。"""
    from gateway import get_db
    return _cc_collect_state(get_db)


def build_cc_one_shot(*, include_wake=True):
    """每轮构建的 transactional one-shot（peek only）。"""
    from gateway import get_db
    return _cc_collect_one_shot(get_db, include_wake=include_wake)


def build_cc_cold_once():
    """仅冷启动构建。"""
    from gateway import get_db
    return _cc_collect_cold_once(get_db)


def build_cc_context(*, include_wake=True, include_cold=True):
    """CC resident 专用结构化上下文。

    static      — 仅 spawn 时进入 system
    cold_once   — 仅冷启动注入（可由 include_cold=False 跳过）
    state       — 按组件差量注入
    one_shot    — 本轮 peek；flush 后 consume
    """
    from gateway import get_db
    out = {
        'static': {
            'persona': read_persona(),
            'stable_note': build_stable_note(),
            'save_instr': _CC_SAVE_INSTR,
        },
        'state': _cc_collect_state(get_db),
        'one_shot': _cc_collect_one_shot(get_db, include_wake=include_wake),
    }
    if include_cold:
        out['cold_once'] = _cc_collect_cold_once(get_db)
    else:
        out['cold_once'] = {}
    return out


def format_state_diff(old_state, new_state):
    """比较两个 state dict，返回差量文本；无变化返回空串。"""
    old_state = old_state or {}
    new_state = new_state or {}
    lines = []
    keys = list(dict.fromkeys(list(old_state.keys()) + list(new_state.keys())))
    for key in keys:
        before = (old_state.get(key) or '').strip()
        after = (new_state.get(key) or '').strip()
        if before == after:
            continue
        label = {
            'time_bucket': '当前时间段',
            'emotion': '情绪',
            'drive': '驱动',
            'lights': '灯',
            'pocket': 'Pocket',
            'todos': '留言板待办',
            'ledger': '记账',
            'reminders': '今日提醒',
            'recent_activity': '最近活动',
        }.get(key, key)
        if before and not after:
            lines.append(f'- {label}：已清空')
        elif not before and after:
            lines.append(f'- {label}：{after}')
        else:
            # 短字段（时间/灯）用 before → after；长字段只报 after
            if key in ('time_bucket', 'lights', 'pocket') and len(before) < 80 and len(after) < 80:
                lines.append(f'- {label}：{before} → {after}')
            else:
                lines.append(f'- {label}：{after}')
    if not lines:
        return ''
    return '【状态更新】\n' + '\n'.join(lines)


def format_state_snapshot(state):
    state = state or {}
    chunks = [v.strip() for v in state.values() if v and str(v).strip()]
    if not chunks:
        return ''
    return '【当前状态】\n' + '\n\n'.join(chunks)


def format_cold_once(cold):
    cold = cold or {}
    chunks = [v.strip() for v in cold.values() if v and str(v).strip()]
    return '\n\n'.join(chunks)


_ONE_SHOT_TEXT_KEYS = ('wake_feedback', 'task_feedback', 'dream_flash')


def format_one_shot(one_shot):
    one_shot = one_shot or {}
    chunks = []
    for key in _ONE_SHOT_TEXT_KEYS:
        val = one_shot.get(key)
        if val and str(val).strip():
            chunks.append(str(val).strip())
    return '\n\n'.join(chunks)
