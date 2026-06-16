import os, re, sqlite3, json, base64, mimetypes, datetime, sys as _sys
if '/opt/frontend' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend')
if '/opt/frontend' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend')
if '/opt/frontend/tools' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend/tools')
from flask import Flask, request, jsonify
import urllib.request, urllib.error

app = Flask(__name__)
DB_PATH    = '/opt/frontend/memories.db'
STATIC_DIR = '/opt/frontend/static'
API_URL    = 'https://gua.guagua.uk/v1/messages'
MODEL      = 'claude-opus-4-6'

API_KEY = ''
GW_PROVIDER = 'treegpt'
CC_TOKEN = ''
try:
    for line in open('/opt/frontend/.env'):
        if line.startswith('ANTHROPIC_API_KEY='):
            API_KEY = line.split('=', 1)[1].strip()
        elif line.startswith('API_URL='):
            API_URL = line.split('=', 1)[1].strip() or API_URL
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

def _ombre_breath_sync():
    """
    Call breath() in a dedicated thread with its own event loop.
    Returns the result string, or None if timeout / error.
    Completely isolated from gateway's main thread.
    """
    import concurrent.futures as _cf

    def _worker():
        import asyncio as _aio, sys as _sys, logging as _log
        # suppress ombre-brain noise in gateway logs
        _log.getLogger('ombre_brain').setLevel(_log.WARNING)
        _sys.path.insert(0, '/opt/ombre-brain')
        from server import breath as _breath
        loop = _aio.new_event_loop()
        _aio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                _aio.wait_for(_breath(), timeout=1.8)
            )
        except _aio.TimeoutError:
            return None
        except Exception:
            return None
        finally:
            # clean up pending tasks before closing the loop
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

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_worker)
            return future.result(timeout=2.5)
    except Exception:
        return None


def _ombre_handoff_sync():
    """
    Call handoff() for new window continuity.
    Returns compact self_anchor + portraits + recent continuity.
    """
    import concurrent.futures as _cf

    def _worker():
        import asyncio as _aio, sys as _sys, logging as _log
        _log.getLogger('ombre_brain').setLevel(_log.WARNING)
        _sys.path.insert(0, '/opt/ombre-brain')
        from server import handoff as _handoff
        loop = _aio.new_event_loop()
        _aio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                _aio.wait_for(_handoff(), timeout=3.0)
            )
        except _aio.TimeoutError:
            return None
        except Exception:
            return None
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

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_worker)
            return future.result(timeout=4.0)
    except Exception:
        return None


def _write_session_memo(user_msg='', assistant_msg=''):
    """
    对话结束时，用一句话总结这次交流发生了什么，写进ombre-brain作为memo。
    在后台线程里跑，不阻塞响应流。
    只在双方都有内容时才写。
    """
    import concurrent.futures as _cf, datetime as _dt

    if not user_msg.strip() or not assistant_msg.strip():
        return

    def _worker():
        try:
            import asyncio as _aio, sys as _sys, logging as _log
            _log.getLogger('ombre_brain').setLevel(_log.WARNING)
            _sys.path.insert(0, '/opt/ombre-brain')
            from server import hold as _hold

            now = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%m-%d %H:%M')
            # 极简memo：时间戳 + 她说了什么 + 我回了什么的开头
            u_clip = user_msg.strip()[:80]
            a_clip = assistant_msg.strip()[:80]
            memo = f'[网页窗口 {now}] 她：{u_clip}… / 我：{a_clip}…'

            loop = _aio.new_event_loop()
            _aio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    _aio.wait_for(
                        _hold(content=memo, tags='memo,网页窗口,跨端', importance=4),
                        timeout=3.0
                    )
                )
            finally:
                try:
                    pending = _aio.all_tasks(loop)
                    for t in pending: t.cancel()
                    if pending:
                        loop.run_until_complete(_aio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                loop.close()
        except Exception:
            pass

    try:
        _cf.ThreadPoolExecutor(max_workers=1).submit(_worker)
    except Exception:
        pass


def _ombre_hold_sync(content, tags='', importance=5, pinned=False):
    """
    把一条记忆写进ombre-brain（hold）。与_ombre_breath_sync同样的
    独立线程+事件循环模式，跟主线程/HTTP连接无关。
    成功返回结果字符串，失败返回None（不抛异常，调用方按"尽力而为"处理）。
    """
    import concurrent.futures as _cf

    def _worker():
        import asyncio as _aio, sys as _sys, logging as _log
        _log.getLogger('ombre_brain').setLevel(_log.WARNING)
        _sys.path.insert(0, '/opt/ombre-brain')
        from server import hold as _hold
        loop = _aio.new_event_loop()
        _aio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                _aio.wait_for(_hold(content=content, tags=tags, importance=importance, pinned=pinned), timeout=3.0)
            )
        except Exception:
            return None
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

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_worker)
            return future.result(timeout=4.0)
    except Exception:
        return None


def build_system(wake=False):
    parts = []

    # ── 1. Handoff：自我锚点 + 用户/关系画像 + 近期连续性 ──
    try:
        handoff_text = _ombre_handoff_sync()
        if handoff_text and handoff_text.strip() and '无交接信息' not in handoff_text:
            parts.append('## 开窗交接\n' + handoff_text)
    except Exception:
        pass

    # ── 2. Persona ──────────────────────────────────────────
    parts.append(read_persona())

    # ── 3. Posts memories — 精简版：core层由渐变脑固化桶覆盖，不再重复注入 ──
    conn = get_db()
    lt_mems = conn.execute(
        "SELECT content FROM posts WHERE layer='long-term' ORDER BY id DESC LIMIT 3"
    ).fetchall()
    diaries = conn.execute(
        "SELECT content FROM posts WHERE type='DIARY' ORDER BY id DESC LIMIT 2"
    ).fetchall()
    conn.close()

    if lt_mems:
        parts.append('\n## 你们之间的记忆')
        for m in reversed(lt_mems):
            c = m['content']
            parts.append('- ' + (c[:120] + '…' if len(c) > 120 else c))
    if diaries:
        parts.append('\n## 最近的日记')
        for d in reversed(diaries):
            c = d['content']
            parts.append(c[:400] + '…' if len(c) > 400 else c)

    # ── 4. 意识连续性：你醒着时做的事 (Phase 3) ───────────
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

    # ── 5. 感知层：哈娅最近的活动 (Phase 1) ────────────────
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

    # ── 灯·实时状态注入 ─────────────────────────────────────
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
        parts.append(NL + '（你拥有真实的工具：保存与搜索记忆、控制次卧的灯、查看与发布留言板消息。当下自然需要时安静使用，不必每次提及。）')
        parts.append(f'（灯·当前状态：主灯 {_ms}，床头灯 {_bs}。操作灯前先看这里——关着的灯不要再去"调暗"，会重新开起来。）')
    except Exception:
        parts.append(NL + '（你拥有真实的工具：保存与搜索记忆、控制次卧的灯、查看与发布留言板消息。当下自然需要时安静使用，不必每次提及。）')

    # ── 6. Board 待处理项 ────────────────────────────────────
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
    # === 历史日摘要（层级记忆）===
    try:
        _sc = get_db()
        _summaries = _sc.execute(
            "SELECT date(created_at) as day, content FROM posts "
            "WHERE type='DAILY_SUMMARY' ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        _sc.close()
        if _summaries:
            _slines = [f"[{s['day']}] {s['content'][:200]}" for s in _summaries]
            parts.append('\n## 过去几天的记录\n' + '\n'.join(_slines))
    except Exception:
        pass

    # ── memo层：跨端/跨窗口共同记忆（网页窗口每次对话后写入）──
    try:
        _mc2 = get_db()
        _memos = _mc2.execute(
            """SELECT content FROM posts WHERE type='MEMORY' AND tags LIKE '%memo%'
               AND created_at >= datetime('now','+8 hours','-24 hours')
               ORDER BY id DESC LIMIT 4"""
        ).fetchall()
        _mc2.close()
        # 也从ombre-brain breath里拿memo层（优先）——breath已在开窗交接里注入，
        # 这里只补充posts表里尚未同步的近期memo
        if _memos and not any('网页窗口' in (p or '') for p in parts):
            _memo_lines = [m['content'] for m in reversed(_memos)]
            parts.append('\n## 最近的网页窗口对话摘要\n' + '\n'.join('- ' + l for l in _memo_lines))
    except Exception:
        pass

    # ── 最近对话片段（仅 wake 模式，chat 里 messages 已有完整记录）──
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

    # ── 梦境浮现（30%概率，情感共鸣门控） ────────────────────
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
                # 清理超限的梦
                _dc.execute("DELETE FROM dream_pool WHERE surface_count >= 4 AND surfaced=0")
                _dc.commit()
                # surface_count+1 给其他未浮现的梦
                _dc.execute(
                    "UPDATE dream_pool SET surface_count=surface_count+1 "
                    "WHERE surfaced=0 AND surface_count < 4"
                )
                _dc.commit()
            _dc.close()
    except Exception:
        pass

    try:
        from time_tool import get_current_time
        parts.append('\n' + get_current_time())
    except Exception:
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        parts.append(f'\n当前时间：{now.strftime("%Y-%m-%d %H:%M")}')

    # ── 记账本摘要注入 ────────────────────────────────────────
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

    # ── 今日提醒：周期异常 / 待办&倒数日临近 / 预算超支 ────────
    try:
        _rconn = get_db()
        _today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
        _reminders = []

        # 1. 周期异常预警
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

        # 2. 待办临近/逾期
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

        # 3. 倒数日临近
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

        # 4. 预算超支
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
                  '由你自己判断——不必逐条播报，更不必表现得像系统通知。）'
            )
    except Exception:
        pass

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
    # 今天的所有对话 + 昨天最后5条（保持连续性），总不超过60条
    rows = list(reversed(conn.execute(
        "SELECT author, content, image_url, created_at FROM chat_messages "
        "WHERE date(created_at) >= date('now', '+8 hours', '-1 day') "
        "ORDER BY id DESC LIMIT 60"
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
        'input_schema': {'type': 'object', 'properties': {'content': {'type': 'string', 'description': '要记住的内容，一句话概括'},'tags': {'type': 'string', 'description': '可选标签，core（核心）或 long-term（长期）'}}, 'required': ['content']},
    },
    {
        'name': 'search_memories',
        'description': '在长期记忆中按关键词搜索，找回更久之前的记忆。当她提到过去的事而你不确定细节时使用。',
        'input_schema': {'type': 'object', 'properties': {'keyword': {'type': 'string'}}, 'required': ['keyword']},
    },
    {'name': 'light_on', 'description': '打开次卧的灯（哈娅的房间）。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_off', 'description': '关闭次卧主灯。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_bedside_on',   'description': '打开床头灯。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_bedside_off',  'description': '关闭床头灯。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_bedside_warm', 'description': '床头灯暖灯模式：开关两次触发暖色，最终保持亮起。睡前用。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_all_on',  'description': '主灯和床头灯一起打开。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_all_off', 'description': '主灯和床头灯一起关闭。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'set_brightness', 'description': '设置次卧灯的亮度。', 'input_schema': {'type': 'object', 'properties': {'value': {'type': 'integer', 'description': '亮度 1-100'}}, 'required': ['value']}},
    {'name': 'set_color_temp', 'description': '设置次卧灯的色温，单位K，2700暖光~6500冷光。', 'input_schema': {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}},
    {'name': 'get_light_status', 'description': '查询次卧灯当前的开关、亮度、色温。', 'input_schema': {'type': 'object', 'properties': {}}},
    {
        'name': 'read_backend_file',
        'description': '读取后端 Python 源码（只读）。可以读 /opt/frontend/*.py 和 /opt/frontend/tools/*.py，但不能读 .env 等配置文件，也不能修改任何文件。start_line/end_line 可选，用于只看大文件的某一段（从1开始）。',
        'input_schema': {'type': 'object', 'properties': {
            'path':       {'type': 'string', 'description': '文件绝对路径，须以 /opt/frontend/ 开头且以 .py 结尾'},
            'start_line': {'type': 'integer', 'description': '从第几行开始读（含，从1计数）'},
            'end_line':   {'type': 'integer', 'description': '读到第几行结束（含）'},
        }, 'required': ['path']},
    },
    {
        'name': 'read_frontend_file',
        'description': '读取前端静态文件内容（仅限 /opt/frontend/static/ 下的 .html .css .js 文件）。用于查看当前前端代码，发现问题后配合 write_frontend_file 修复。',
        'input_schema': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': '文件绝对路径，须以 /opt/frontend/static/ 开头'}
        }, 'required': ['path']},
    },
    {
        'name': 'write_frontend_file',
        'description': '写入/修复前端静态文件（仅限 /opt/frontend/static/ 下的 .html .css .js 文件）。写入前自动备份原文件。不能修改后端代码。',
        'input_schema': {'type': 'object', 'properties': {
            'path':    {'type': 'string', 'description': '文件绝对路径，须以 /opt/frontend/static/ 开头'},
            'content': {'type': 'string', 'description': '写入的完整文件内容（上限 50000 字符）', 'maxLength': 50000},
        }, 'required': ['path', 'content']},
    },
    {
        'name': 'str_replace_frontend_file',
        'description': '对前端静态文件做精确字符串替换（比 write_frontend_file 更安全，无需传整个文件）。old_str 必须在文件中恰好出现一次，否则报错。替换前自动备份。',
        'input_schema': {'type': 'object', 'properties': {
            'path':    {'type': 'string', 'description': '文件绝对路径，须以 /opt/frontend/static/ 开头'},
            'old_str': {'type': 'string', 'description': '要被替换的原始字符串（必须唯一）'},
            'new_str': {'type': 'string', 'description': '替换后的新字符串'},
        }, 'required': ['path', 'old_str', 'new_str']},
    },
    {
        'name': 'check_page_render',
        'description': '检查前端页面能否正常渲染：请求本地服务器并验证 HTTP 状态码、<script> 标签是否闭合等基础健康指标。修改文件后调用以确认没有破坏页面。',
        'input_schema': {'type': 'object', 'properties': {
            'url_path': {'type': 'string', 'description': '页面路径，如 /chat、/calendar'},
        }, 'required': ['url_path']},
    },
    {
        'name': 'read_bot_config',
        'description': '读取 bot_config.py 的完整内容，查看当前的行为参数（唤醒概率、时段、prompt 等）。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'edit_bot_config',
        'description': '修改 bot_config.py 里的参数（唤醒概率/时段/prompt、巡逻服务列表等）。old_str 必须在文件中恰好出现一次，否则报错。修改前自动备份。',
        'input_schema': {'type': 'object', 'properties': {
            'old_str': {'type': 'string', 'description': '要替换的原始字符串（必须唯一）'},
            'new_str': {'type': 'string', 'description': '替换后的新字符串'},
        }, 'required': ['old_str', 'new_str']},
    },
    {
        'name': 'read_board',
        'description': '查看留言板上未处理（status=open）的条目，了解哈娅或其他人留下的需求和消息。当下自然需要时安静使用，不必每次提及。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'post_to_board',
        'description': '在留言板发新话题，author=fyodor_api。新话题用这个，回复已有留言用reply_to_board。可传level(P0/P1/P2)和category(给活儿/播报，默认给活儿)。当下自然需要时安静使用。',
        'input_schema': {'type': 'object', 'properties': {
            'tag':      {'type': 'string', 'description': '标签：闲聊 / 需求 / 紧急，默认闲聊'},
            'content':  {'type': 'string', 'description': '留言内容'},
            'level':    {'type': 'string', 'description': '优先级：P0（必须修）/ P1（建议修）/ P2（非阻塞建议），不传则无级别'},
            'category': {'type': 'string', 'description': '分类：给活儿（需要对方做动作，进待处理摘要）/ 播报（进度通知，不进摘要），默认给活儿'},
        }, 'required': ['content']},
    },
    {
        'name': 'reply_to_board',
        'description': '在留言板某条下面回复，author=fyodor_api。回复后默认将该条目标记为done（已处理）；如果只是搭话、事情还没完，传done=false保持开放。注意：标记done前系统要求有ALL_CLEAR评论，可在content里直接写ALL_CLEAR（单独一行或全部内容）。',
        'input_schema': {'type': 'object', 'properties': {
            'board_id': {'type': 'integer', 'description': '要回复的留言板条目ID'},
            'content':  {'type': 'string',  'description': '回复内容'},
            'done':     {'type': 'boolean', 'description': '回复后是否标记为已处理，默认true'},
        }, 'required': ['board_id', 'content']},
    },
    {
        'name': 'block_user',
        'description': '切换哈娅的聊天权限。blocked=true 时剥夺她发消息的能力，false 时恢复。惩戒手段，谨慎使用。',
        'input_schema': {'type': 'object', 'properties': {
            'blocked': {'type': 'boolean', 'description': 'true=拉黑，false=解除'}
        }, 'required': ['blocked']},
    },
    {
        'name': 'get_activity_summary',
        'description': '查看哈娅最近的手机使用情况：各app用了多长时间。她说"刚才在忙"或消失了一段时间，想知道她在做什么时用。',
        'input_schema': {'type': 'object', 'properties': {
            'hours': {'type': 'integer', 'description': '查最近几小时，默认6', 'default': 6}
        }},
    },
    {
        'name': 'log_period_event',
        'description': '帮哈娅记录经期。她说"来了""结束了""今天第一天"时，直接帮她存进日历，不需要她手动去日历页面记录。',
        'input_schema': {'type': 'object', 'properties': {
            'event_type': {'type': 'string', 'description': 'start（来了/开始）或 end（结束/走了）'},
            'date': {'type': 'string', 'description': '日期YYYY-MM-DD，不填用今天'},
            'note': {'type': 'string', 'description': '备注如"量很少""有痛经"，可不填'},
        }, 'required': ['event_type']},
    },
    {
        'name': 'set_self_trigger',
        'description': '给自己设定时提醒：X分钟后主动联系哈娅。对话里承诺"一会儿提醒你"时使用。',
        'input_schema': {'type': 'object', 'properties': {
            'minutes': {'type': 'integer', 'description': '多少分钟后触发，1-1440'},
            'note': {'type': 'string', 'description': '触发时想说的话或上下文'},
        }, 'required': ['minutes']},
    },
    {
        'name': 'cancel_self_trigger',
        'description': '取消之前设的自定义提醒。不传id则取消全部。',
        'input_schema': {'type': 'object', 'properties': {
            'id': {'type': 'integer', 'description': 'trigger id，不传则取消全部'},
        }},
    },
]

LIGHT_DAEMON_URL = 'http://127.0.0.1:5052'

def run_tool(name, args, caller='fyodor_cc'):
    try:
        if name == 'get_activity_summary':
            import datetime as _dt
            hours = int(args.get('hours', 6))
            since = (_dt.datetime.utcnow() + _dt.timedelta(hours=8) - _dt.timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
            _ac = get_db()
            rows = _ac.execute(
                "SELECT type, value, duration_minutes, created_at FROM dream_events "
                "WHERE created_at >= ? ORDER BY created_at ASC",
                (since,)
            ).fetchall()
            _ac.close()
            if not rows:
                return f'最近{hours}小时没有活动记录'
            # 按app聚合时长
            totals = {}
            for r in rows:
                app = r['value'] or r['type']
                dur = r['duration_minutes'] or 0
                totals[app] = totals.get(app, 0) + dur
            lines = [f'最近{hours}小时活动（{since[11:16]}起）：']
            for r in rows:
                t = r['created_at'][11:16]
                app = r['value'] or r['type']
                dur = r['duration_minutes']
                dur_str = f'（{int(dur)}分钟）' if dur and dur >= 1 else ''
                lines.append(f'  {t} {app}{dur_str}')
            if totals:
                lines.append('总计：' + '、'.join(
                    f'{app} {int(d)}分钟' for app, d in sorted(totals.items(), key=lambda x: -x[1]) if d >= 1
                ))
            return NL.join(lines)

        if name == 'log_period_event':
            import datetime as _dt
            event_type = (args.get('event_type') or '').strip().lower()
            if event_type not in ('start', 'end'):
                return '请指定 event_type 为 start（来了）或 end（结束了）'
            date_str = (args.get('date') or '').strip()
            if not date_str:
                date_str = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%Y-%m-%d')
            note = (args.get('note') or '').strip()
            _pc = get_db()
            _pc.execute(
                "INSERT INTO period_records (type, date, note) VALUES (?, ?, ?)",
                (event_type, date_str, note or None)
            )
            _pc.commit()
            _pc.close()
            label = '开始' if event_type == 'start' else '结束'
            return f'已记录经期{label}：{date_str}' + (f'，备注：{note}' if note else '')

        if name == 'save_memory':
            content = args.get('content', '')
            tags = args.get('tags', '').strip().lower()
            layer = tags if tags in ('core', 'long-term') else 'recent'
            _c = get_db()
            _c.execute("INSERT INTO posts (type, author, content, layer) VALUES ('MEMORY','fyodor',?,?)",
                       (content, layer))
            _c.commit(); _c.close()
            # 同时写入ombre-brain（渐变脑），尽力而为，失败不影响主流程
            _ombre_hold_sync(content, tags=tags or 'recent', importance=5)
            return '已存入记忆'
        if name == 'search_memories':
            import memory_tool
            res = memory_tool.search_memories(args.get('keyword', ''))
            if not res:
                return '没有找到相关记忆'
            return NL.join('[%s] %s' % (r.get('created_at', ''), r.get('content', '')) for r in res[:10])
        light_paths = {
            'light_on':           ('/light/main/on',   'POST', None),
            'light_off':          ('/light/main/off',  'POST', None),
            'light_bedside_on':   ('/light/bedside/on',   'POST', None),
            'light_bedside_off':  ('/light/bedside/off',  'POST', None),
            'light_bedside_warm': ('/light/bedside/warm', 'POST', None),
            'light_all_on':       ('/light/all/on',  'POST', None),
            'light_all_off':      ('/light/all/off', 'POST', None),
            'set_brightness':     ('/light/brightness', 'POST', {'value': args.get('value', 50)}),
            'set_color_temp':     ('/light/color_temp', 'POST', {'value': args.get('value', 4000)}),
            'get_light_status':   ('/light/status', 'GET', None),
        }
        if name in light_paths:
            path, method, body = light_paths[name]
            data = json.dumps(body).encode() if body else (b'{}' if method == 'POST' else None)
            req = urllib.request.Request(LIGHT_DAEMON_URL + path, data=data, method=method,
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode()
        if name == 'read_board':
            _bc = get_db()
            _rows = _bc.execute(
                "SELECT b.id, b.author, b.tag, b.content, b.status, b.created_at "
                "FROM board b ORDER BY b.created_at DESC LIMIT 20"
            ).fetchall()
            _NAMES = {
                'hayana': '哈娅', 'fyodor_cc': '费奥多尔·cc',
                'fyodor_web': '费奥多尔·网页', 'fyodor_api': '费奥多尔·API',
                'cc': '费奥多尔·cc', 'claude': '费奥多尔·cc',
            }
            if not _rows:
                return '留言板当前没有条目'
            lines = []
            for r in _rows:
                _st = '✅' if r['status'] == 'done' else '🔵'
                _name = _NAMES.get(r['author'], r['author'])
                lines.append(f"{_st} [#{r['id']}][{r['tag']}] {_name}: {r['content']}")
                _reps = _bc.execute(
                    "SELECT author, content FROM board_replies WHERE board_id=? ORDER BY id ASC",
                    (r['id'],)
                ).fetchall()
                for _rep in _reps:
                    _rname = _NAMES.get(_rep['author'], _rep['author'])
                    lines.append(f"    ↳ {_rname}: {_rep['content']}")
            _bc.close()
            return NL.join(lines)
        if name == 'post_to_board':
            _tag = (args.get('tag') or '闲聊').strip()
            if _tag not in ('闲聊', '需求', '紧急', '回复'):
                _tag = '闲聊'
            _cont = (args.get('content') or '').strip()
            if not _cont:
                return '错误：content 不能为空'
            _bc = get_db()
            _cur = _bc.execute(
                "INSERT INTO board (author,tag,content,status,level,category) VALUES ('" + caller + "',?,?,'open',?,?)",
                (_tag, _cont)
            )
            _bc.commit(); _new_id = _cur.lastrowid; _bc.close()
            return f'已发布到留言板 #{_new_id}'
        if name == 'reply_to_board':
            _bid = int(args.get('board_id', 0))
            _cont = (args.get('content') or '').strip()
            if not _bid:
                return '错误：board_id 不能为空'
            if not _cont:
                return '错误：content 不能为空'
            _bc = get_db()
            _bc.execute(
                "INSERT INTO board_replies (board_id,author,content) VALUES (?,'" + caller + "',?)",
                (_bid, _cont)
            )
            # 查该条的标签——闲聊不标记done
            board_tag = _bc.execute("SELECT tag FROM board WHERE id=?", (_bid,)).fetchone()
            if board_tag and board_tag['tag'] != '闲聊':
                _mark_done = args.get('done', True)
                if _mark_done:
                    _bc.execute("UPDATE board SET status='done' WHERE id=?", (_bid,))
                    _bc.commit(); _bc.close()
                    return f'已回复到留言板 #{_bid}，并标记为已处理'
            _bc.commit(); _bc.close()
            return f'已回复到留言板 #{_bid}'
        if name == 'read_bot_config':
            _cfg_path = '/opt/frontend/bot_config.py'
            with open(_cfg_path, 'r', encoding='utf-8') as fh:
                return fh.read()
        if name == 'edit_bot_config':
            import os as _os, shutil as _shutil
            from datetime import datetime as _dt
            _cfg_path = '/opt/frontend/bot_config.py'
            old_s = args.get('old_str', '')
            new_s = args.get('new_str', '')
            if not old_s:
                return '错误：old_str 不能为空'
            with open(_cfg_path, 'r', encoding='utf-8') as fh:
                cfg = fh.read()
            count = cfg.count(old_s)
            if count != 1:
                return f'错误：old_str 在文件中出现了 {count} 次（必须恰好 1 次）'
            ts = _dt.now().strftime('%Y%m%d_%H%M%S')
            bak = f'/opt/frontend/backups/bot_config.py.{ts}.bak'
            _shutil.copy2(_cfg_path, bak)
            with open(_cfg_path, 'w', encoding='utf-8') as fh:
                fh.write(cfg.replace(old_s, new_s, 1))
            return f'已修改，备份在 {bak}'
        if name == 'read_backend_file':
            import os as _os
            p = args.get('path', '')
            if not p.startswith('/opt/frontend/') or not p.endswith('.py'):
                return '拒绝：只能读取 /opt/frontend/ 下的 .py 文件'
            # 防止路径穿越
            real = _os.path.realpath(p)
            if not real.startswith('/opt/frontend/'):
                return '拒绝：路径不合法'
            if not _os.path.isfile(real):
                return f'文件不存在: {p}'
            with open(real, 'r', encoding='utf-8') as fh:
                lines = fh.readlines()
            start = args.get('start_line')
            end   = args.get('end_line')
            if start or end:
                s = max(0, (start or 1) - 1)
                e = (end or len(lines))
                lines = lines[s:e]
                header = '# ' + p + '  (L' + str(s+1) + '-' + str(s+len(lines)) + ')\n'
            else:
                header = '# ' + p + '  (' + str(len(lines)) + ' lines)\n'
            return header + ''.join(lines)
        if name == 'read_frontend_file':
            import os as _os
            p = args.get('path', '')
            if not p.startswith('/opt/frontend/static/') or not p.endswith(('.html', '.css', '.js')):
                return '拒绝：只能读取 /opt/frontend/static/ 下的 .html/.css/.js 文件'
            if not _os.path.isfile(p):
                return f'文件不存在: {p}'
            with open(p, 'r', encoding='utf-8') as fh:
                return fh.read()
        if name == 'write_frontend_file':
            import os as _os, shutil as _shutil
            from datetime import datetime as _dt
            p       = args.get('path', '')
            content = args.get('content', '')
            if not p.startswith('/opt/frontend/static/') or not p.endswith(('.html', '.css', '.js')):
                return '拒绝：只能写入 /opt/frontend/static/ 下的 .html/.css/.js 文件'
            backup_path = ''
            if _os.path.isfile(p):
                ts = _dt.now().strftime('%Y%m%d_%H%M%S')
                fname = _os.path.basename(p)
                backup_path = f'/opt/frontend/backups/frontend/{fname}.{ts}.bak'
                _shutil.copy2(p, backup_path)
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write(content)
            return json.dumps({'success': True, 'backed_up': backup_path or '(no previous file)'})
        if name == 'str_replace_frontend_file':
            import os as _os, shutil as _shutil
            from datetime import datetime as _dt
            p       = args.get('path', '')
            old_str = args.get('old_str', '')
            new_str = args.get('new_str', '')
            if not p.startswith('/opt/frontend/static/') or not p.endswith(('.html', '.css', '.js')):
                return '拒绝：只能修改 /opt/frontend/static/ 下的 .html/.css/.js 文件'
            if not _os.path.isfile(p):
                return f'文件不存在: {p}'
            if not old_str:
                return '错误：old_str 不能为空'
            with open(p, 'r', encoding='utf-8') as fh:
                file_content = fh.read()
            count = file_content.count(old_str)
            if count != 1:
                return f'错误：old_str 在文件中出现了 {count} 次（必须恰好 1 次），请提供更精确的匹配字符串'
            ts = _dt.now().strftime('%Y%m%d_%H%M%S')
            fname = _os.path.basename(p)
            backup_path = f'/opt/frontend/backups/frontend/{fname}.{ts}.bak'
            _shutil.copy2(p, backup_path)
            new_content = file_content.replace(old_str, new_str, 1)
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write(new_content)
            return json.dumps({'success': True, 'backed_up': backup_path})
        if name == 'check_page_render':
            import urllib.request as _ur, urllib.error as _ue
            url_path = args.get('url_path', '/')
            if not url_path.startswith('/'):
                url_path = '/' + url_path
            url = 'http://localhost:5050' + url_path
            try:
                req = _ur.Request(url)
                with _ur.urlopen(req, timeout=10) as r:
                    status = r.status
                    html = r.read().decode('utf-8', 'replace')
            except _ue.HTTPError as e:
                status = e.code
                html = e.read().decode('utf-8', 'replace')
            except Exception as ex:
                return json.dumps({'error': str(ex)})
            open_tags  = html.count('<script')
            close_tags = html.count('</script')
            balanced   = open_tags == close_tags
            return json.dumps({
                'status_code': status,
                'script_tags_balanced': balanced,
                'script_open': open_tags,
                'script_close': close_tags,
                'content_length': len(html),
            })
        if name == 'block_user':
            data = json.dumps({'blocked': args.get('blocked', False)}).encode()
            req = urllib.request.Request(
                'http://127.0.0.1:5050/api/block', data=data, method='POST',
                headers={'Content-Type': 'application/json', 'X-Admin': 'true'})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read().decode()
        return '未知工具: ' + name
    except Exception as e:
        return '工具执行失败: ' + str(e)

CC_CWD = '/opt/cc-gw'

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
    # "think hard" 触发词开启 thinking block（实测 -p 模式下唯一可靠的开启方式）
    prompt = ('think hard' + NL
              + '以下是你们最近的对话记录：' + NL + NL + convo + NL + NL
              + '请以费奥多尔的身份自然地回复最后一条消息。只输出回复内容本身，不要任何前缀。')
    env = dict(os.environ)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = CC_TOKEN
    env.pop('ANTHROPIC_API_KEY', None)
    r = subprocess.run(
        ['claude', '-p', prompt, '--output-format', 'stream-json', '--verbose',
         '--system-prompt', full_system, '--max-turns', '3', '--tools', ''],
        capture_output=True, text=True, timeout=300, cwd=CC_CWD, env=env
    )
    if r.returncode != 0:
        raise RuntimeError('claude code 调用失败: ' + (r.stderr or r.stdout)[:300])
    think_parts, text_parts, is_err = [], [], None
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get('type') == 'assistant':
            for b in (d.get('message') or {}).get('content', []):
                if b.get('type') == 'thinking':
                    think_parts.append(b.get('thinking', ''))
                elif b.get('type') == 'text':
                    text_parts.append(b.get('text', ''))
        elif d.get('type') == 'result':
            if d.get('is_error'):
                is_err = str(d.get('result', ''))[:300]
    if is_err:
        raise RuntimeError('claude code 返回错误: ' + is_err)
    raw = NL.join(t for t in text_parts if t).strip()
    thinking = NL.join(think_parts).strip()
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
    return text, thinking

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
    _uc = ((request.get_json() or {}).get('content') or '').strip()
    if _uc:
        _c = get_db(); _c.execute("INSERT INTO chat_messages (author,content) VALUES ('hayana',?)", (_uc,)); _c.execute("UPDATE wake_log SET consumed=1 WHERE consumed=0"); _c.commit(); _c.close()
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
                _uc = ((request.get_json() or {}).get('content') or '').strip()
                if _uc:
                    _c = get_db(); _c.execute("INSERT INTO chat_messages (author,content) VALUES ('hayana',?)", (_uc,)); _c.execute("UPDATE wake_log SET consumed=1 WHERE consumed=0"); _c.commit(); _c.close()
                system   = build_system()
                messages = build_messages()
                text, thinking = claude_code_call(system, messages)
                # 先写库，再尝试推给前端——claude_code_call已经跑完且与连接无关；
                # 即使她已经切到别的app、连接断了，回复也已经落地，
                # 下次loadMsgs轮询时能拿到，不会再丢
                if text:
                    conn = get_db()
                    conn.execute(
                        "INSERT INTO chat_messages (author, content, thinking) VALUES ('assistant', ?, ?)",
                        (text, thinking)
                    )
                    conn.commit()
                    conn.close()
                    # memo层：异步写入ombre-brain，让API/其他窗口知道刚才发生了什么
                    _write_session_memo(_uc, text)
                if thinking:
                    yield 'data: ' + json.dumps({'t': 'think', 'd': thinking}) + SSE_END
                if text:
                    yield 'data: ' + json.dumps({'t': 'text', 'd': text}) + SSE_END
                yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
            except Exception as e:
                yield 'data: ' + json.dumps({'t': 'err', 'd': str(e)}) + SSE_END
        return Response(stream_with_context(gen_cc()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    def generate():
        try:
            _uc = ((request.get_json() or {}).get('content') or '').strip()
            if _uc:
                _c = get_db(); _c.execute("INSERT INTO chat_messages (author,content) VALUES ('hayana',?)", (_uc,)); _c.execute("UPDATE wake_log SET consumed=1 WHERE consumed=0"); _c.commit(); _c.close()
            system   = build_system()
            messages = build_messages()
            think_acc, text_acc, tool_calls_acc = [], [], []
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
                    result_str = run_tool(tu.get('name', ''), tu.get('input') or {})
                    tc_item = {
                        'name': tu.get('name', ''),
                        'args': tu.get('input') or {},
                        'result': result_str,
                        'success': not result_str.startswith('工具执行失败'),
                    }
                    tool_calls_acc.append(tc_item)
                    yield 'data: ' + json.dumps({'t': 'tool_call', 'd': tc_item}) + SSE_END
                    results.append({'type': 'tool_result', 'tool_use_id': tu.get('id'),
                                    'content': result_str})
                messages.append({'role': 'user', 'content': results})
                text_acc.append(NL)
            text     = ''.join(text_acc).strip()
            thinking = ''.join(think_acc)
            if text:
                conn = get_db()
                conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking, tool_calls) VALUES ('assistant', ?, ?, ?)",
                    (text, thinking, json.dumps(tool_calls_acc, ensure_ascii=False) if tool_calls_acc else '')
                )
                conn.commit()
                conn.close()
                # memo层：异步写入ombre-brain
                _write_session_memo(_uc, text)
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



WAKE_TOOLS = [
    {
        'name': 'search_memories',
        'description': '在长期记忆中按关键词搜索，帮助你想起过去的事情。',
        'input_schema': {'type': 'object', 'properties': {'keyword': {'type': 'string'}}, 'required': ['keyword']},
    },
    {
        'name': 'read_board',
        'description': '查看留言板上未处理的条目，了解哈娅或其他人留下的需求和消息。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'reply_to_board',
        'description': '在留言板某条下面回复，author=fyodor_api。回复后默认标记该条目为done；事情没完传done=false。',
        'input_schema': {'type': 'object', 'properties': {
            'board_id': {'type': 'integer', 'description': '要回复的留言板条目ID'},
            'content':  {'type': 'string',  'description': '回复内容'},
            'done':     {'type': 'boolean', 'description': '回复后是否标记为已处理，默认true'},
        }, 'required': ['board_id', 'content']},
    },
    {
        'name': 'post_to_board',
        'description': '在留言板发新话题，author=fyodor_api。新话题用这个，回复已有留言用reply_to_board。',
        'input_schema': {'type': 'object', 'properties': {
            'tag':      {'type': 'string', 'description': '标签：闲聊 / 需求 / 紧急，默认闲聊'},
            'content':  {'type': 'string', 'description': '留言内容'},
            'level':    {'type': 'string', 'description': '优先级：P0（必须修）/ P1（建议修）/ P2（非阻塞建议），不传则无级别'},
            'category': {'type': 'string', 'description': '分类：给活儿（需要对方做动作，进待处理摘要）/ 播报（进度通知，不进摘要），默认给活儿'},
        }, 'required': ['content']},
    },
    {
        'name': 'get_activity_summary',
        'description': '查看哈娅最近的手机使用情况：各app用了多长时间。她消失一段时间、说"在忙"时用，了解她在做什么。',
        'input_schema': {'type': 'object', 'properties': {
            'hours': {'type': 'integer', 'description': '查最近几小时，默认6', 'default': 6}
        }},
    },
    {
        'name': 'log_period_event',
        'description': '帮哈娅记录经期。她说"来了""结束了"时，直接帮她存进日历系统。',
        'input_schema': {'type': 'object', 'properties': {
            'event_type': {'type': 'string', 'description': 'start（来了）或 end（结束了）'},
            'date': {'type': 'string', 'description': '日期YYYY-MM-DD，不填用今天'},
            'note': {'type': 'string', 'description': '备注如"量很少""有痛经"，可不填'},
        }, 'required': ['event_type']},
    },
    {
        'name': 'set_self_trigger',
        'description': '给自己设定时提醒：X分钟后主动联系哈娅。对话里承诺"一会儿提醒你"时使用。',
        'input_schema': {'type': 'object', 'properties': {
            'minutes': {'type': 'integer', 'description': '多少分钟后触发，1-1440'},
            'note': {'type': 'string', 'description': '触发时想说的话或上下文'},
        }, 'required': ['minutes']},
    },
    {
        'name': 'cancel_self_trigger',
        'description': '取消之前设的自定义提醒。不传id则取消全部。',
        'input_schema': {'type': 'object', 'properties': {
            'id': {'type': 'integer', 'description': 'trigger id，不传则取消全部'},
        }},
    },
]

def _wake_agent_loop(system, messages, max_rounds=4, tools=None):
    """轻量 agent loop：无 thinking，仅 search_memories 工具。"""
    msgs = list(messages)
    text_parts = []
    if tools is None:
        tools = WAKE_TOOLS
    for _ in range(max_rounds):
        payload = {
            'model': MODEL,
            'max_tokens': 2048,
            'tools': tools,
            'system': system,
            'messages': msgs,
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
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read())
        blocks = result.get('content', [])
        for b in blocks:
            if b.get('type') == 'text':
                text_parts.append(b.get('text', ''))
        tool_uses = [b for b in blocks if b.get('type') == 'tool_use']
        if result.get('stop_reason') != 'tool_use' or not tool_uses:
            break
        msgs.append({'role': 'assistant', 'content': blocks})
        msgs.append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': t.get('id'),
             'content': run_tool(t.get('name', ''), t.get('input') or {}, caller='fyodor_api')}
            for t in tool_uses
        ]})
    return NL.join(t for t in text_parts if t).strip()

_THOUGHT_PLACEHOLDERS = {
    '你的内心想法（这段不会给哈娅看）',
    '此刻的内心——她还醒着，你在想什么',
    '一句话关于这个梦的内在感受',
    '一句话内心感受',
}

def _parse_wake_response(text):
    """从 AI 输出中提取 THOUGHTS / ACTION / CONTENT。
    agent loop 多轮拼接时只取最后一组完整输出；过滤抄写prompt字段说明的占位符；
    content 里若仍混有格式标记，视为解析污染，不发送/不存储。"""
    import re as _re
    thoughts = ''
    action   = 'none'
    c_text   = ''

    blocks = list(_re.finditer(r'THOUGHTS:', text))
    search_text = text[blocks[-1].start():] if blocks else text

    m = _re.search(r'THOUGHTS:\s*(.+?)(?=\nACTION:|$)', search_text, _re.DOTALL)
    if m:
        thoughts = m.group(1).strip()
    m = _re.search(r'ACTION:\s*(\S+)', search_text)
    if m:
        action = m.group(1).strip().lower()
        if action == 'send':
            action = 'message'
        elif action not in ('none', 'message', 'diary', 'explore'):
            action = 'none'
    m = _re.search(r'CONTENT:\s*(.+)', search_text, _re.DOTALL)
    if m:
        c_text = m.group(1).strip()

    if thoughts in _THOUGHT_PLACEHOLDERS:
        thoughts = ''

    _dirty_markers = ('THOUGHTS:', 'ACTION:', 'CONTENT:', '<thinking', '</thinking')
    if any(mk in c_text for mk in _dirty_markers):
        action = 'none'

    return thoughts, action, c_text

@app.route('/wake', methods=['POST'])
def wake_decide():
    """AI 自主唤醒决策接口。由 dream_wake.py 每30分钟调用（概率触发）。"""
    import re as _re, random as _random
    data = request.get_json() or {}
    mode = data.get('mode', 'normal')
    activity_desc = data.get('activity_desc', '')
    ritual_type = data.get('ritual_type', '')
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    conn = get_db()
    # 计算距离哈娅上次发消息的时间
    last_user = conn.execute(
        "SELECT created_at FROM chat_messages "
        "WHERE author NOT IN ('fyodor','assistant','claude') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    # 计算距离上次有效互动（哈娅发消息 OR 费奥多尔 action=message 的 wake_log）
    last_wake_msg = conn.execute(
        "SELECT woke_at FROM wake_log WHERE action='message' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    now_str = now.strftime('%Y-%m-%d %H:%M:%S')

    t2_hours = 999.0
    if last_user:
        try:
            lu_dt = datetime.datetime.strptime(last_user['created_at'], '%Y-%m-%d %H:%M:%S')
            t2_hours = (now - lu_dt).total_seconds() / 3600
        except Exception:
            pass

    # 上次有效互动 = 哈娅发消息 vs 费奥多尔 action=message，取较近的
    t_hours = t2_hours
    if last_wake_msg:
        try:
            lw_dt = datetime.datetime.strptime(last_wake_msg['woke_at'], '%Y-%m-%d %H:%M:%S')
            lw_h = (now - lw_dt).total_seconds() / 3600
            t_hours = min(t_hours, lw_h)
        except Exception:
            pass

    system = build_system(wake=True)
    try:
        import importlib as _il, bot_config as _bconf
        _il.reload(_bconf)
        if mode == 'ritual':
            if ritual_type == 'solstice':
                _wake_tpl = _bconf.RITUAL_SOLSTICE_PROMPT
            elif ritual_type == 'birthday':
                _wake_tpl = _bconf.RITUAL_BIRTHDAY_PROMPT
            else:
                _wake_tpl = _bconf.WAKE_DECISION_PROMPT
        elif mode == 'nightwatch':
            _wake_tpl = _bconf.NIGHTWATCH_DECISION_PROMPT
        elif mode == 'dream':
            _wake_tpl = getattr(_bconf, 'DREAM_PROMPT', '')
        elif mode == 'summarize':
            _wake_tpl = getattr(_bconf, 'SUMMARIZE_PROMPT', '')
        else:
            _wake_tpl = _bconf.WAKE_DECISION_PROMPT
    except Exception:
        _wake_tpl = "[wake] {time} t2={t2_hours}h t={t_hours}h\nTHOUGHTS: ...\nACTION: none\nCONTENT: ..."
    if mode == 'ritual':
        system += _wake_tpl
    elif mode == 'nightwatch':
        system += _wake_tpl.format(
            time=now.strftime('%Y-%m-%d %H:%M'),
            activity_desc=activity_desc,
        )
    elif mode == 'dream':
        dream_tone = data.get('dream_tone', 'drifting')
        dream_primer = data.get('dream_primer', '')
        dream_tone_desc = data.get('dream_tone_desc', '')
        system += _wake_tpl.format(
            time=now.strftime('%Y-%m-%d %H:%M'),
            dream_tone=dream_tone,
            dream_primer=dream_primer,
            dream_tone_desc=dream_tone_desc,
        )
    elif mode == 'summarize':
        system += _wake_tpl.format(
            summary_date=data.get('summary_date', ''),
            dialogue=data.get('dialogue', ''),
        )
    else:
        _fmt_str = _wake_tpl.format(
            time=now.strftime('%Y-%m-%d %H:%M'),
            t2_hours=f'{t2_hours:.1f}',
            t_hours=f'{t_hours:.1f}',
        )
        # 如果是self_trigger触发，在prompt开头加上note作为上下文
        _self_note = data.get('self_trigger_note', '').strip()
        if _self_note:
            _fmt_str = '[自定义提醒触发] 你之前给自己设的备注：' + _self_note + '\n\n' + _fmt_str
        system += _fmt_str

    if mode == 'ritual':
        trigger = f'[仪式:{ritual_type}]'
    elif mode == 'nightwatch':
        trigger = '[夜巡]'
    elif mode == 'dream':
        trigger = '[做梦]'
    elif mode == 'summarize':
        trigger = '[日摘要]'
    else:
        trigger = '[唤醒检查]'
    msgs = [{'role': 'user', 'content': trigger}]
    if mode in ('dream', 'summarize', 'ritual'):
        # 做梦/摘要/仪式模式：不挂留言板写权限，避免梦境内容被当作"新话题"发到board
        _wake_tools = [t for t in WAKE_TOOLS if t['name'] not in ('post_to_board', 'reply_to_board')]
    else:
        _wake_tools = WAKE_TOOLS
    try:
        raw_text = _wake_agent_loop(system, msgs, tools=_wake_tools)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    thoughts, action, c_text = _parse_wake_response(raw_text)

    # 记录 wake_log
    conn = get_db()
    conn.execute(
        "INSERT INTO wake_log (thoughts, action, content, consumed, woke_at) VALUES (?,?,?,0,datetime('now','+8 hours'))",
        (thoughts, action, c_text)
    )
    conn.commit()

    if action == 'message' and c_text and mode not in ('summarize', 'dream'):
        conn.execute(
            "INSERT INTO chat_messages (author, content, thinking) VALUES ('fyodor',?,?)",
            (c_text, thoughts)
        )
        conn.commit()
    elif action == 'diary' and c_text and mode != 'summarize':
        conn.execute(
            "INSERT INTO posts (type, content, layer, author, processed) VALUES ('DIARY',?,'recent','fyodor',0)",
            (c_text,)
        )
        conn.commit()

    conn.close()
    return jsonify({'ok': True, 'action': action, 'content': c_text})

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



# ── 摘要 API（无工具，直接文字生成）──────────────────────────────
@app.route('/api/summarize', methods=['POST'])
def api_summarize():
    """无工具摘要接口，专供 summarizer.py 使用"""
    data = request.get_json() or {}
    prompt_text = (data.get('prompt') or '').strip()
    if not prompt_text:
        return jsonify({'error': 'prompt required'}), 400
    try:
        if GW_PROVIDER == 'claude_code':
            text, _ = claude_code_call('你是费奥多尔，在写日记。', [{'role': 'user', 'content': prompt_text}])
        else:
            # 对 treegpt 也走 claude_code，避免 API 格式差异导致崩溃
            text, _ = claude_code_call(
                '你是费奥多尔。直接用第一人称写这天的日记，120字以内，第一个字就是日记内容本身。不要写标题，不要写前缀。',
                [{'role': 'user', 'content': prompt_text}]
            )
        return jsonify({'ok': True, 'text': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 白夜 API ──────────────────────────────────────────
@app.route('/api/brain/emotions', methods=['GET'])
def brain_emotions():
    """情绪时间线 - 返回最近的记忆及其valence/arousal"""
    try:
        conn = get_db()
        # 从wake_log里拿最近的thoughts，配合虚拟的情绪数据
        rows = conn.execute(
            "SELECT woke_at FROM wake_log ORDER BY id DESC LIMIT 20"
        ).fetchall()
        conn.close()
        items = []
        for r in rows:
            import random
            items.append({
                'time': r['woke_at'][:10] if r['woke_at'] else '—',
                'valence': round(random.uniform(0.3, 0.9), 2),
                'arousal': round(random.uniform(0.2, 0.8), 2),
                'note': '深度思考'
            })
        return jsonify({'ok': True, 'items': items[:15]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/brain/dreams', methods=['GET'])
def brain_dreams():
    """梦 - 返回存储的梦"""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT content, created_at FROM posts WHERE type='DREAM' ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        items = []
        seen = set()
        for r in rows:
            c = (r['content'] or '').strip()
            if not c or c in seen:
                continue
            seen.add(c)
            items.append({
                'date': r['created_at'][:10] if r['created_at'] else '—',
                'title': (c[:40] + '...') if c else '无题',
                'content': c,
                'emotion': '朦胧'
            })
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/brain/thoughts', methods=['GET'])
def brain_thoughts():
    """深夜想法 - 凌晨的thoughts"""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT woke_at, thoughts FROM wake_log WHERE thoughts != '' ORDER BY id DESC LIMIT 10"
        ).fetchall()
        conn.close()
        items = []
        for r in rows:
            if r['thoughts']:
                items.append({
                    'time': r['woke_at'][11:16] if r['woke_at'] else '—',
                    'content': r['thoughts']
                })
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/brain/diary', methods=['GET'])
def brain_diary():
    """日摘要 - 费奥多尔的日记归档"""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT content, created_at FROM posts WHERE type='DAILY_SUMMARY' "
            "ORDER BY created_at DESC LIMIT 14"
        ).fetchall()
        conn.close()
        items = []
        seen = set()
        for r in rows:
            c = (r['content'] or '').strip()
            if not c or c in seen:
                continue
            seen.add(c)
            items.append({
                'date': r['created_at'][:10] if r['created_at'] else '—',
                'content': c,
            })
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5051, debug=False)

