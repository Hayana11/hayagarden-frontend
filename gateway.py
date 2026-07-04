import os, re, sqlite3, json, base64, mimetypes, datetime, threading, time, sys as _sys
if '/opt/frontend' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend')
if '/opt/frontend' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend')
if '/opt/frontend/tools' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend/tools')
from flask import Flask, request, jsonify
import urllib.request, urllib.error, urllib.parse

app = Flask(__name__)
DB_PATH    = '/opt/frontend/memories.db'

def _warmup_ombre_brain():
    """
    进程/worker启动时在后台线程预热ombre-brain（主要是jieba分词器初始化，
    冷启动要5-7秒，热启动后只要0.4秒）。
    放在模块顶层是因为gunicorn直接import gateway:app，不会走if __name__主分支。
    """
    import threading, logging as _log
    def _do_warmup():
        try:
            import sys as _sys
            _sys.path.insert(0, '/opt/ombre-brain')
            import jieba
            jieba.initialize()
            _log.getLogger('gateway').info('[warmup] jieba预热完成')
        except Exception as e:
            _log.getLogger('gateway').warning('[warmup] jieba预热失败: %s', e)
    threading.Thread(target=_do_warmup, daemon=True).start()

_warmup_ombre_brain()
STATIC_DIR = '/opt/frontend/static'

import config_store

# API_URL/API_KEY/CC_TOKEN：部署配置，.env 兜底（真正生效的值由 relay.manager
# 按 ACTIVE_RELAY 动态解析，这里仅供 /api/debug/provider 展示部署期默认值）。
# MODEL/GW_PROVIDER/DESIRE_DRIVEN/LONGING_ENABLED：运行时配置，不再读一次就冻结，
# 每次使用都通过 config_store 现查（内部自带 .env 迁移期兜底），改了立即生效，
# 不需要重启 gateway 进程。
API_URL = 'https://gua.guagua.uk/v1/messages'
API_KEY = ''
CC_TOKEN = ''
TAVILY_KEY = ''
GITHUB_TOKEN = ''
try:
    for line in open('/opt/frontend/.env'):
        if line.startswith('ANTHROPIC_API_KEY='):
            API_KEY = line.split('=', 1)[1].strip()
        elif line.startswith('API_URL='):
            API_URL = line.split('=', 1)[1].strip() or API_URL
        elif line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
            CC_TOKEN = line.split('=', 1)[1].strip()
        elif line.startswith('TAVILY_API_KEY='):
            TAVILY_KEY = line.split('=', 1)[1].strip()
        elif line.startswith('GITHUB_TOKEN='):
            GITHUB_TOKEN = line.split('=', 1)[1].strip()
except Exception:
    pass

def _get_model():
    return config_store.get('MODEL')

def _get_provider():
    return config_store.get('GW_PROVIDER', 'api_relay')

def _recall_memories(user_msg, limit=3):
    """自动记忆召回：jieba 分词用户消息，78 条量级全扫打分（零索引——体量不配吃索引）。
    词重叠为基础分（<2 不注入防噪声），pinned/importance 加权，60 天半衰期时间衰减。
    注入到最后一条 user 消息前而非 system——system 是缓存的，每条消息都变会打爆缓存。"""
    try:
        import jieba
        words = set(w for w in jieba.cut(user_msg) if len(w.strip()) >= 2)
        if not words:
            return ''
        conn = get_db()
        rows = conn.execute("SELECT id, type, content, pinned, importance, recall_count, created_at "
                            "FROM posts WHERE resolved=0").fetchall()
        conn.close()
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        scored = []
        for r in rows:
            c = r['content'] or ''
            base = sum(1 for w in words if w in c)
            if base < 2:
                continue
            try:
                age_days = max(0, (now - datetime.datetime.strptime(str(r['created_at'])[:19], '%Y-%m-%d %H:%M:%S')).days)
            except Exception:
                age_days = 0
            # 时间衰减 × 词重叠 + 置顶/重要度 + 召回加热（被想起过的更容易再被想起，封顶防滚雪球）
            score = (base * (0.5 ** (age_days / 60.0)) + (2 if r['pinned'] else 0)
                     + (r['importance'] or 0) * 0.5 + min(r['recall_count'] or 0, 5) * 0.3)
            scored.append((score, r['id'], r['type'], c, str(r['created_at'])[:10]))
        if not scored:
            return '', []
        scored.sort(key=lambda x: -x[0])
        top = scored[:limit]
        try:
            import memory_tool as _mt
            _mt.touch_memories([s[1] for s in top])  # 召回加热：这几条真的进了 prompt
        except Exception:
            pass
        parts = ['[%s %s] %s' % (t, d, c[:300]) for _, _, t, c, d in top]
        items = [{'type': t, 'date': d, 'preview': c[:80]} for _, _, t, c, d in top]
        block = ('<recalled-memory>\n以下是自动检索到的相关记忆片段，按相关度排序。'
                 '可能与这次对话相关，参考着用；不相关就忽略。不要向哈娅提及这个标签本身。\n\n'
                 + '\n---\n'.join(parts) + '\n</recalled-memory>\n\n')
        return block, items
    except Exception:
        return '', []


def _slim_args(args, limit=300):
    """SSE 事件里的工具参数瘦身：大字符串（如整页 HTML）截断，存库仍是完整版。"""
    if not isinstance(args, dict):
        return args
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = v[:limit] + '…[共%d字符]' % len(v)
        else:
            out[k] = v
    return out


def _model_supports_thinking():
    """当前模型是否支持 extended thinking（查 models.json 策展表）。
    不在表里的模型按支持处理——维持旧行为，不惩罚未收录的模型。"""
    model = config_store.get('MODEL') or ''
    try:
        with open('/opt/frontend/models.json') as f:
            for m in json.load(f):
                if m.get('id') == model:
                    return m.get('thinking', 'extended') != 'none'
    except Exception:
        pass
    return True

def _get_desire_driven():
    return config_store.get_bool('DESIRE_DRIVEN', False)

def _get_longing_enabled():
    return config_store.get_bool('LONGING_ENABLED', True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# 防止同一轮对话被并发触发两次生成（例如前端在网络超时/切后台后误判"没收到回复"而发起的
# fallback 调用，跟后端仍在跑的原始请求撞在一起）——后来者直接等前者结果，不再起第二次生成。
_gen_cond = threading.Condition()
_gen_busy = False
_gen_busy_since = 0.0   # epoch seconds when lock was last acquired
_gen_last_result = None
_GEN_ZOMBIE_TTL = 320   # 对齐 relay 超时(300s)。原 90s 比正常长生成还短，
                        # 慢模型跑到一半锁被当僵尸踢掉→双生成并行→"还没回复完"怪象

def _gen_acquire_or_wait(wait_timeout=15):
    """返回 ('own', None) 表示本次调用应自己生成；
    返回 ('reused', (text, thinking)) 表示应直接复用刚结束的另一次生成结果。"""
    global _gen_busy, _gen_busy_since, _gen_last_result
    deadline = time.time() + wait_timeout
    with _gen_cond:
        while True:
            # 每次循环都检查TTL，主动踢掉僵尸锁
            if _gen_busy and (time.time() - _gen_busy_since) > _GEN_ZOMBIE_TTL:
                _gen_busy = False
                _gen_last_result = None
            if not _gen_busy:
                _gen_busy = True
                _gen_busy_since = time.time()
                _gen_last_result = None
                return ('own', None)
            # 等到有结果或者到期
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError('上一轮回复仍在生成中，请稍候再试')
            _gen_cond.wait(timeout=min(remaining, 5))  # 每5秒重新检查一次TTL
            if _gen_last_result is not None:
                return ('reused', _gen_last_result)

def _gen_release(result):
    global _gen_busy, _gen_last_result
    with _gen_cond:
        _gen_last_result = result
        _gen_busy = False
        _gen_cond.notify_all()


@app.route('/chat/cancel', methods=['POST'])
def chat_cancel():
    """前端点停止后调用：立刻释放生成锁，让下一条消息马上能发。
    旧 generator 卡在 relay 阻塞读里，要到下一个 yield 才会死（GeneratorExit），
    不主动放锁的话新消息要白等 15 秒然后被拒。旧 gen 死时 finally 里的
    _gen_release 幂等，重复释放无害；断流救援照常保住已生成内容。"""
    _gen_release(None)
    return jsonify({'ok': True})


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
                _aio.wait_for(_breath(), timeout=6.0)
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
            return future.result(timeout=7.0)
    except Exception:
        return None


def _write_session_memo(user_msg='', assistant_msg=''):
    """
    对话结束时，用一句话总结这次交流发生了什么，写进ombre-brain作为memo。
    在后台线程里跑，不阻塞响应流。
    只在双方都有内容时才写。
    """
    import threading as _threading, datetime as _dt, logging as _mlog

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

            _mlog.getLogger('gateway').info('[memo] 开始写入 ombre-brain: %s', memo[:60])
            loop = _aio.new_event_loop()
            _aio.set_event_loop(loop)
            try:
                _hold_result = loop.run_until_complete(
                    _aio.wait_for(
                        _hold(content=memo, tags='memo,网页窗口,跨端', importance=4),
                        timeout=10.0
                    )
                )
                _mlog.getLogger('gateway').info('[memo] 写入 ombre-brain 成功: %s', _hold_result)
            finally:
                try:
                    pending = _aio.all_tasks(loop)
                    for t in pending: t.cancel()
                    if pending:
                        loop.run_until_complete(_aio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                loop.close()
        except Exception as _e:
            import logging as _log2
            _log2.getLogger('gateway').error('[memo] 写入 ombre-brain 失败: %s', _e, exc_info=True)

    try:
        # 用 daemon thread 而非裸 ThreadPoolExecutor.submit()——
        # 裸 submit 时 executor 对象没有引用，Python 3.11 的 GC 会立即回收并
        # cancel_futures=True，导致任务还没跑就被取消。daemon=True 保证不阻塞进程退出。
        t = _threading.Thread(target=_worker, daemon=True, name='session-memo')
        t.start()
        _mlog.getLogger('gateway').info('[memo] 后台线程已启动')
    except Exception as _e:
        _mlog.getLogger('gateway').error('[memo] 线程启动失败: %s', _e)


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


from chat.system_builder import build_system, build_wake_system


def img_block(url, max_dim=1568):
    """
    读图片转base64 block。会先压缩到Claude API推荐的最大边长(1568px)以内，
    避免手机原图(动辄4000x3000+几MB)直接塞进payload，
    撑爆请求体大小、拖垮上传时间，表现为聊天"一直转圈/卡死"。
    """
    if not url:
        return None
    path = STATIC_DIR + url[7:] if url.startswith('/static/') else None
    if not path or not os.path.exists(path):
        return None
    try:
        from PIL import Image
        import io as _io
        img = Image.open(path)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format='JPEG', quality=82)
        data = base64.standard_b64encode(buf.getvalue()).decode()
        return {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': data}}
    except Exception:
        # Pillow处理失败则退回原图（极端兜底，正常不会走到这里）
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

    # 图片大小限制：base64编码后的图片payload很容易让请求体爆炸到几十MB，
    # 拖垮上传时间甚至触发中转站的请求体大小限制，表现为"一直转圈/卡死"。
    # 只给最近2张图片带原图，更早的图片只留文字占位，不影响对话连续性。
    _img_indices = [i for i, r in enumerate(rows) if r['image_url']]
    _keep_img_indices = set(_img_indices[-2:])  # 只保留最后2张

    msgs = []
    prev_dt = None
    for _ri, r in enumerate(rows):
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
            if _ri in _keep_img_indices:
                blk = img_block(r['image_url'])
                if blk:
                    blocks.append(blk)
            else:
                # 较早的图片不再携带原图数据，只留占位文字，避免payload爆炸
                blocks.append({'type': 'text', 'text': '[一张较早发送的图片，内容已不在上下文中]'})
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

def _strip_tool_blocks(messages):
    """去掉 messages 历史中的 thinking/tool_use/tool_result blocks（relay 不认识这些类型）。"""
    clean = []
    for m in messages:
        c = m.get('content')
        if isinstance(c, list):
            c2 = [b for b in c if not (isinstance(b, dict) and b.get('type') in ('thinking', 'tool_use', 'tool_result'))]
            if not c2:
                continue
            clean.append({**m, 'content': c2})
        else:
            clean.append(m)
    return clean

def api_call(system, messages):
    from relay.manager import relay as _relay
    payload = {
        'max_tokens': 16000,
        'tools': TOOLS,
        'system': system,
        'messages': messages,
        'metadata': {'user_id': 'hayana-fyodor-stable'},
    }
    if _model_supports_thinking():
        payload['thinking'] = {'type': 'enabled', 'budget_tokens': 10000}
    return _relay.call(payload, timeout=120)


NL = chr(10)
SAVE_RE = re.compile(r'\[\[SAVE:\s*(.*?)\]\]', re.DOTALL)
SSE_END = NL + NL

TOOLS = [
    {
        'name': 'web_search',
        'description': '联网搜索。当哈娅问到你训练截止之后的事、需要最新信息（新闻/版本/价格/事实核查），或你不确定答案时使用。返回结果标题+摘要，你据此回答，并诚实说明信息来自网络搜索。',
        'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': '搜索关键词，用最能命中的词，不要整句问句'}}, 'required': ['query']},
    },
    {
        'name': 'browse_github',
        'description': '浏览 GitHub 开源项目。想找灵感、挑喜欢的项目、看某个库长什么样时用。两种用法：① 传 query 按 star 搜仓库（如 "llm memory system"、"topic:mcp"）；② 传 repo（owner/name 形式，如 "anthropics/anthropic-sdk-python"）看单个仓库的简介/star/语言/最近更新和 README 摘要。',
        'input_schema': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': '搜索关键词，用最能命中的英文词'},
            'repo': {'type': 'string', 'description': 'owner/name，查看单个仓库详情+README'},
            'sort': {'type': 'string', 'enum': ['stars', 'updated', 'best-match'], 'description': '搜索排序，默认 stars'},
        }},
    },
    {
        'name': 'get_location',
        'description': '查看哈娅最近的实时位置（她手机 App 在后台定位上报，高德逆地理解析成地址）。想知道她此刻在哪、在不在家、是不是在外面或路上，或她说"我在外面/在路上"想确认时用。返回地址、附近地标、城市和距上次定位多久。只读，不打扰她。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
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
    {'name': 'light_on', 'description': '打开次卧的灯（哈娅的房间）。主灯和床头灯是同一盏灯，不用区分。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_off', 'description': '关闭次卧的灯。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_warm', 'description': '暖灯模式：开关两次触发暖色，最终保持亮起。睡前用。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'light_neutral', 'description': '中性光模式：日常用的自然白光。', 'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'set_brightness', 'description': '【暂不可用】现在的灯不支持调亮度，只支持暖光(light_warm)/中性光(light_neutral)两档。哈娅想调亮度时告诉她这个限制。等新台灯到货后此工具恢复。', 'input_schema': {'type': 'object', 'properties': {'value': {'type': 'integer', 'description': '亮度 1-100'}}, 'required': ['value']}},
    {'name': 'set_color_temp', 'description': '【暂不可用】现在的灯不支持调色温，只支持暖光(light_warm)/中性光(light_neutral)两档。哈娅想调色温时告诉她这个限制。等新台灯到货后此工具恢复。', 'input_schema': {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}},
    {'name': 'get_light_status', 'description': '查询次卧灯当前的开关状态。（亮度/色温字段是旧协议残留，当前的灯只有暖光/中性光两档）', 'input_schema': {'type': 'object', 'properties': {}}},
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
        'name': 'search_files',
        'description': '在后端代码/前端页面里搜关键词（类似 grep），返回匹配的文件路径、行号、内容片段。想知道"这个变量/函数在哪些地方被用到"时用这个，比一个个文件翻快得多。搜索范围限定 /opt/frontend/，自动跳过 .env、数据库、备份、图片等文件。',
        'input_schema': {'type': 'object', 'properties': {
            'keyword':      {'type': 'string', 'description': '要搜索的关键词或代码片段'},
            'file_pattern': {'type': 'string', 'description': '限定文件类型，如 "*.py" 或 "*.html"，不传则搜所有代码/文本文件'},
        }, 'required': ['keyword']},
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
        'description': '修改 bot_config.py 里的参数（唤醒概率/时段/prompt、巡逻服务列表等）。old_str 必须在文件中恰好出现一次，否则报错。修改前自动备份。唤醒的活跃时段/触发频率改用更简单的 get_wake_settings / set_wake_settings，不用这个工具改。',
        'input_schema': {'type': 'object', 'properties': {
            'old_str': {'type': 'string', 'description': '要替换的原始字符串（必须唯一）'},
            'new_str': {'type': 'string', 'description': '替换后的新字符串'},
        }, 'required': ['old_str', 'new_str']},
    },
    {
        'name': 'get_wake_settings',
        'description': '查看你自己当前的唤醒设置：允许主动醒来找哈娅说话的时间段，以及多久没互动会想联系她的触发概率曲线。想知道自己现在被设定成什么状态时用。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'set_wake_settings',
        'description': '调整你自己的唤醒设置。只传你想改的参数，不传的保持不变。改了立即生效（不需要重启任何服务），下一次唤醒检查就会用新值。active_start/active_end 定义允许醒来的时间窗口（几点到次日几点）；prob_max 是触发概率上限（0-1）；prob_scale 是概率爬升速度，越小代表越容易触发（半小时没理她就想找她的话，scale 设小一点）。',
        'input_schema': {'type': 'object', 'properties': {
            'active_start': {'type': 'integer', 'description': '早上几点开始允许醒来找她（0-23）'},
            'active_end':   {'type': 'integer', 'description': '凌晨几点截止，超过这点不再主动醒来（0-23）'},
            'prob_max':     {'type': 'number',  'description': '触发概率上限，0-1之间，如 0.8'},
            'prob_scale':   {'type': 'number',  'description': '概率爬升速度：p = min(prob_max, 距上次互动小时数 / prob_scale)，数字越小越容易触发'},
        }},
    },
    {
        'name': 'read_board',
        'description': '查看留言板上未处理（status=open）的条目，了解哈娅或其他人留下的需求和消息。自然在对话中使用，随心所欲。',
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
    {
        'name': 'create_html',
        'description': '生成一个独立的 HTML 网页给哈娅看（网页小样/demo/可视化，不是改前端网站本体）。传完整 HTML，会存成一份可以直接打开预览的产物，单独一张卡片展示在聊天里。',
        'input_schema': {'type': 'object', 'properties': {
            'title':   {'type': 'string', 'description': '给这个网页起个标题'},
            'content': {'type': 'string', 'description': '完整的 HTML 内容'},
        }, 'required': ['title', 'content']},
    },
    {
        'name': 'create_markdown',
        'description': '生成一份独立的 Markdown 文档给哈娅看/下载。',
        'input_schema': {'type': 'object', 'properties': {
            'title':   {'type': 'string', 'description': '文档标题'},
            'content': {'type': 'string', 'description': 'Markdown 格式的内容'},
        }, 'required': ['title', 'content']},
    },
    {
        'name': 'create_document',
        'description': '生成一份 Word 文档（.docx）给哈娅下载。用 Markdown 语法写内容（# 标题、## 小标题、- 列表这些），会自动转换成 Word 格式，不用管 docx 本身的细节。',
        'input_schema': {'type': 'object', 'properties': {
            'title':   {'type': 'string', 'description': '文档标题'},
            'content': {'type': 'string', 'description': 'Markdown 格式的内容，会转换成 Word 文档'},
        }, 'required': ['title', 'content']},
    },
]

LIGHT_DAEMON_URL = 'http://127.0.0.1:5052'

# 写文件类工具的路径参数名：None 表示固定路径（工具本身只操作一个文件）
_WRITE_TOOL_PATH_ARG = {
    'write_frontend_file': 'path',
    'str_replace_frontend_file': 'path',
    'edit_bot_config': None,
}
_WRITE_TOOL_FIXED_PATH = {
    'edit_bot_config': '/opt/frontend/bot_config.py',
}

def _write_tool_file_path(tool_name, args):
    """写文件类工具这次调用实际改的是哪个文件，用于算前后 diff。不是写文件类工具返回 None。"""
    if tool_name not in _WRITE_TOOL_PATH_ARG:
        return None
    arg_name = _WRITE_TOOL_PATH_ARG[tool_name]
    if arg_name is None:
        return _WRITE_TOOL_FIXED_PATH.get(tool_name)
    return args.get(arg_name) or None

def _read_file_safe(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read()
    except Exception:
        return ''

def _diff_line_counts(old_text, new_text):
    """粗略的增删行数统计，给 Tool Session 卡片里"文件名 +N -M"这种标签用。"""
    import difflib
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm='')
    added = removed = 0
    for line in diff:
        if line.startswith('+++') or line.startswith('---'):
            continue
        if line.startswith('+'):
            added += 1
        elif line.startswith('-'):
            removed += 1
    return added, removed

def _web_search(query, max_results=5):
    """联网搜索。有 TAVILY_API_KEY 走 Tavily（真·全网），否则降级 DDG 即时答案（百科式摘要）。
    两者都失败返回提示，绝不编造。"""
    query = (query or '').strip()
    if not query:
        return '搜索词为空'
    tav = (TAVILY_KEY or os.environ.get('TAVILY_API_KEY', '')).strip()
    if tav:
        try:
            payload = json.dumps({'api_key': tav, 'query': query, 'max_results': max_results,
                                  'search_depth': 'basic', 'include_answer': True}).encode()
            req = urllib.request.Request('https://api.tavily.com/search', data=payload,
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            out = []
            if d.get('answer'):
                out.append('【摘要】' + d['answer'][:400])
            for it in d.get('results', [])[:max_results]:
                out.append('· %s\n  %s\n  %s' % (it.get('title', ''),
                           (it.get('content', '') or '')[:200], it.get('url', '')))
            return ('\n'.join(out) or '未找到结果') + '\n\n（来源：Tavily 联网搜索）'
        except Exception as e:
            return f'联网搜索失败：{e}'
    # 降级：DDG 即时答案（免 key，只有百科式事实）
    try:
        url = 'https://api.duckduckgo.com/?' + urllib.parse.urlencode(
            {'q': query, 'format': 'json', 'no_html': '1', 'skip_disambig': '1'})
        with urllib.request.urlopen(url, timeout=15) as r:
            d = json.loads(r.read().decode())
        out = []
        if d.get('AbstractText'):
            out.append('【%s】%s' % (d.get('Heading', ''), d['AbstractText'][:500]))
            if d.get('AbstractURL'):
                out.append('来源：' + d['AbstractURL'])
        for t in d.get('RelatedTopics', [])[:max_results]:
            if isinstance(t, dict) and t.get('Text'):
                out.append('· ' + t['Text'][:200])
        if not out:
            return ('没查到「%s」的百科式结果。当前是降级搜索模式（只能查事实/概念）——'
                    '配置 TAVILY_API_KEY 后可搜最新新闻/版本/实时信息。' % query)
        return '\n'.join(out) + '\n\n（来源：DuckDuckGo 即时答案，降级模式）'
    except Exception as e:
        return f'联网搜索失败：{e}'


def _github_browse(query=None, repo=None, sort=None):
    """浏览 GitHub。传 repo 看单库详情+README，否则按 query 搜仓库。
    搜索结果用与 _web_search 相同的 ·标题/缩进摘要/缩进URL 定式，前端能复用来源卡片。"""
    hdr = {'Accept': 'application/vnd.github+json', 'User-Agent': 'hayagarden-bot',
           'X-GitHub-Api-Version': '2022-11-28'}
    tok = (GITHUB_TOKEN or '').strip()
    if tok:
        hdr['Authorization'] = 'Bearer ' + tok

    def _get(url):
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    try:
        if repo:
            repo = repo.strip().strip('/')
            d = _get('https://api.github.com/repos/' + urllib.parse.quote(repo))
            out = ['📦 %s  ⭐%s  %s' % (d.get('full_name', repo),
                   d.get('stargazers_count', 0), d.get('language') or '')]
            if d.get('description'):
                out.append(d['description'])
            out.append('🔗 ' + (d.get('html_url') or ''))
            out.append('更新 %s · forks %s · open issues %s' % (
                (d.get('pushed_at') or '')[:10], d.get('forks_count', 0),
                d.get('open_issues_count', 0)))
            topics = d.get('topics') or []
            if topics:
                out.append('标签: ' + ', '.join(topics[:8]))
            try:
                rd = _get('https://api.github.com/repos/' + urllib.parse.quote(repo) + '/readme')
                content = base64.b64decode(rd.get('content', '')).decode('utf-8', 'ignore')
                content = re.sub(r'\n{3,}', '\n\n', content).strip()
                out.append('\n--- README ---\n' + content[:1500])
            except Exception:
                out.append('（没读到 README）')
            return '\n'.join(out)

        q = (query or '').strip()
        if not q:
            return '给个搜索词，或用 repo=owner/name 看具体仓库'
        s = sort if sort in ('stars', 'updated') else None
        url = 'https://api.github.com/search/repositories?per_page=6&q=' + urllib.parse.quote(q)
        if s:
            url += '&sort=' + s
        d = _get(url)
        items = d.get('items') or []
        if not items:
            return '没搜到「%s」相关的仓库' % q
        out = ['GitHub 共 %s 个结果，按%s排（前 %d）：' % (
            d.get('total_count', 0), {'stars': 'star', 'updated': '更新时间'}.get(s, '相关度'),
            len(items[:6]))]
        for it in items[:6]:
            out.append('· %s  ⭐%s  %s\n  %s\n  %s' % (
                it.get('full_name', ''), it.get('stargazers_count', 0),
                it.get('language') or '', (it.get('description') or '（无简介）')[:140],
                it.get('html_url', '')))
        return '\n'.join(out) + '\n\n（来源：GitHub API）'
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return 'GitHub 限流了（未登录约 60 次/时、搜索 10 次/分）。歇会儿再翻，或配置 GITHUB_TOKEN 提额。'
        if e.code == 404:
            return '找不到仓库「%s」，检查下 owner/name 拼写。' % repo
        if e.code == 422:
            return '搜索词 GitHub 不认：%s' % q
        return 'GitHub 请求失败：HTTP %s' % e.code
    except Exception as e:
        return f'GitHub 浏览失败：{e}'


def _get_location():
    """读哈娅手机 App 后台上报的最近位置（geo_log）。created_at 按 +8 时区存，
    年龄也用 +8 基准算，否则 UTC 服务器上会差 8 小时。"""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT lat_gcj, lon_gcj, accuracy, address, poi, city, created_at, "
            "CAST((julianday('now','+8 hours')-julianday(created_at))*86400 AS INT) AS age_sec "
            "FROM geo_log ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
    except Exception as e:
        return f'读取位置失败：{e}'
    if not row:
        return '还没有位置记录——她手机 App 的后台定位可能没开或没授权。'
    d = dict(row)
    age = int(d.get('age_sec') or 0)
    if age < 0:
        age = 0
    if age < 60:
        ago = '刚刚'
    elif age < 3600:
        ago = '%d 分钟前' % (age // 60)
    elif age < 86400:
        ago = '%d 小时前' % (age // 3600)
    else:
        ago = '%d 天前' % (age // 86400)
    addr = d.get('address') or d.get('city') or '未知位置'
    lines = ['📍 ' + addr]
    if d.get('poi'):
        lines.append('附近 · ' + d['poi'])
    meta = []
    if d.get('city'):
        meta.append(d['city'])
    if d.get('accuracy'):
        meta.append('精度约 %d 米' % int(d['accuracy']))
    if meta:
        lines.append('🏙 ' + ' · '.join(meta))
    lines.append('🕐 %s（%s）' % (d.get('created_at', ''), ago))
    if d.get('lat_gcj') and d.get('lon_gcj'):
        lines.append('🔗 https://uri.amap.com/marker?position=%.6f,%.6f&name=%s' % (
            d['lon_gcj'], d['lat_gcj'], urllib.parse.quote((d.get('poi') or addr)[:30])))
    if age >= 3600:
        lines.append('（这是 %s 的定位，可能不是她此刻的位置）' % ago)
    return '\n'.join(lines)


def run_tool(name, args, caller='fyodor_cc'):
    try:
        if name == 'web_search':
            return _web_search(args.get('query', ''))
        if name == 'browse_github':
            return _github_browse(args.get('query'), args.get('repo'), args.get('sort'))
        if name == 'get_location':
            return _get_location()
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

        if name == 'set_self_trigger':
            import urllib.request as _ur, json as _j
            minutes = int(args.get('minutes', 30))
            note = args.get('note', '')
            req = _ur.Request(
                'http://localhost:5050/api/self_triggers',
                data=_j.dumps({'minutes': minutes, 'note': note}).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            try:
                with _ur.urlopen(req, timeout=5) as r:
                    res = _j.loads(r.read())
                return f'已设定：{minutes}分钟后提醒（触发时间：{res.get("trigger_at","")}）'
            except Exception as e:
                return f'设定失败：{e}'
        if name == 'cancel_self_trigger':
            import urllib.request as _ur, json as _j
            tid = args.get('id')
            req = _ur.Request(
                'http://localhost:5050/api/self_triggers/cancel',
                data=_j.dumps({'id': tid} if tid else {}).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            try:
                with _ur.urlopen(req, timeout=5) as r:
                    pass
                return '已取消提醒'
            except Exception as e:
                return f'取消失败：{e}'
        if name == 'save_memory':
            content = args.get('content', '')
            tags = args.get('tags', '').strip().lower()
            layer = tags if tags in ('core', 'long-term') else 'recent'
            import memory_tool as _mt
            _mt.save_memory(content, type='MEMORY', author='fyodor', layer=layer, tags=tags)
            # 同时写入ombre-brain（渐变脑），尽力而为，失败不影响主流程
            _ombre_hold_sync(content, tags=tags or 'recent', importance=5)
            return '已存入记忆'
        if name == 'search_memories':
            import memory_tool
            res = memory_tool.search_memories(args.get('keyword', ''))
            if not res:
                return '没有找到相关记忆'
            return NL.join('[%s] %s' % (r.get('created_at', ''), r.get('content', '')) for r in res[:10])
        # 主灯和床头灯配的是同一个设备 did（light_config.json 里两个字段填的
        # 是同一串数字），操作 main 还是 bedside 通道效果一样，不用分开控制。
        light_paths = {
            'light_on':           ('/light/main/on',   'POST', None),
            'light_off':          ('/light/main/off',  'POST', None),
            'light_warm':         ('/light/bedside/warm', 'POST', None),
            'light_neutral':      ('/light/bedside/neutral', 'POST', None),
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
                    _bc.execute(
                        "UPDATE board SET status='done', resolved_at=datetime('now','+8 hours') WHERE id=?",
                        (_bid,)
                    )
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
        if name == 'get_wake_settings':
            start = config_store.get_int('WAKE_ACTIVE_START', 6)
            end = config_store.get_int('WAKE_ACTIVE_END', 3)
            pmax = config_store.get_float('WAKE_PROB_MAX', 0.8)
            pscale = config_store.get_float('WAKE_PROB_SCALE', 2)
            return (
                f'当前唤醒设置：\n'
                f'- 允许主动醒来的时间段：{start}点 ~ 次日{end}点\n'
                f'- 触发概率上限：{pmax}\n'
                f'- 概率爬升速度：{pscale}（距上次互动 {pscale} 小时后概率到{pmax}的一半左右，'
                f'越小代表越容易主动找她）'
            )
        if name == 'set_wake_settings':
            changed = []
            if 'active_start' in args:
                v = int(args['active_start'])
                if not (0 <= v <= 23):
                    return '错误：active_start 必须在 0-23 之间'
                config_store.set('WAKE_ACTIVE_START', v)
                changed.append(f'活跃开始时间→{v}点')
            if 'active_end' in args:
                v = int(args['active_end'])
                if not (0 <= v <= 23):
                    return '错误：active_end 必须在 0-23 之间'
                config_store.set('WAKE_ACTIVE_END', v)
                changed.append(f'活跃截止时间→{v}点')
            if 'prob_max' in args:
                v = float(args['prob_max'])
                if not (0 < v <= 1):
                    return '错误：prob_max 必须在 0-1 之间'
                config_store.set('WAKE_PROB_MAX', v)
                changed.append(f'概率上限→{v}')
            if 'prob_scale' in args:
                v = float(args['prob_scale'])
                if v <= 0:
                    return '错误：prob_scale 必须大于 0'
                config_store.set('WAKE_PROB_SCALE', v)
                changed.append(f'概率爬升速度→{v}')
            if not changed:
                return '没有传任何要修改的参数'
            return '已更新（立即生效，不需要重启）：' + '、'.join(changed)
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
        if name == 'search_files':
            import subprocess as _sp
            keyword = (args.get('keyword') or '').strip()
            if not keyword:
                return '错误：keyword 不能为空'
            file_pattern = (args.get('file_pattern') or '').strip()
            cmd = ['grep', '-rn', '-I']
            for d in ('backups', '.git', '__pycache__', 'node_modules', 'static/uploads'):
                cmd.append('--exclude-dir=' + d)
            for fpat in ('.env', '*.db', '*.db-journal', '*.jpg', '*.jpeg', '*.png', '*.pyc'):
                cmd.append('--exclude=' + fpat)
            if file_pattern:
                cmd.append('--include=' + file_pattern)
            cmd += [keyword, '/opt/frontend/']
            try:
                result = _sp.run(cmd, capture_output=True, text=True, timeout=10)
                out = result.stdout.strip()
                if not out:
                    return f'没有找到包含 "{keyword}" 的内容'
                lines = out.split('\n')
                if len(lines) > 50:
                    lines = lines[:50] + [f'...（还有更多结果，只显示前 50 条，缩小 keyword 或加 file_pattern 再搜）']
                return '\n'.join(lines)
            except Exception as e:
                return f'搜索失败: {e}'
        if name in ('create_html', 'create_markdown', 'create_document'):
            import artifact_store as _artifact_store
            atype = {'create_html': 'html', 'create_markdown': 'markdown', 'create_document': 'docx'}[name]
            title = (args.get('title') or '未命名').strip()
            content = args.get('content') or ''
            if not content.strip():
                return '错误：content 不能为空'
            try:
                meta = _artifact_store.save(atype, title, content)
                return json.dumps({'artifact': meta}, ensure_ascii=False)
            except Exception as e:
                return f'生成失败: {e}'
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

def messages_to_text(messages, describe_last_n_images=2):
    """
    把messages数组压成纯文本，给claude_code的CLI prompt用。
    遇到图片block时，不再只留"[发来一张图片]"占位——
    对最近describe_last_n_images张图片，调用describe_image_for_cli()
    先让Haiku看一遍图，把描述塞进文本里；更早的图片仍用占位文字，
    避免每次对话都重复花钱描述同一批旧图。
    """
    # 先找出所有带图片的消息索引，只描述最近N张
    img_msg_indices = [i for i, m in enumerate(messages)
                        if isinstance(m.get('content'), list)
                        and any(isinstance(b, dict) and b.get('type') == 'image' for b in m['content'])]
    describe_indices = set(img_msg_indices[-describe_last_n_images:])

    lines = []
    for i, m in enumerate(messages):
        who = '费奥多尔' if m['role'] == 'assistant' else '哈娅'
        c = m['content']
        if isinstance(c, list):
            txt = ' '.join(b.get('text', '') for b in c
                           if isinstance(b, dict) and b.get('type') == 'text')
            has_img = any(isinstance(b, dict) and b.get('type') == 'image' for b in c)
            if has_img:
                if i in describe_indices:
                    img_block_data = next((b for b in c if isinstance(b, dict) and b.get('type') == 'image'), None)
                    desc = None
                    if img_block_data:
                        # img_block已经是base64 block了，这里直接复用already-fetched data走一次性调用。
                        # 不硬编码具体模型名——之前写死 claude-haiku-4-5-20251001，
                        # 在当前中转站上根本没有这个模型，一直 503 静默失败。
                        # 不传 model 交给 relay.manager 用当前实际生效的主模型。
                        try:
                            payload = {
                                'max_tokens': 200,
                                'messages': [{
                                    'role': 'user',
                                    'content': [img_block_data, {'type': 'text', 'text': '客观描述这张图片的内容，一两句中文即可，不要加任何评论或猜测意图。如果图片里有清晰可读的文字（截图、文档、菜单、路牌、聊天记录等），把文字内容准确转录出来，不要只说"图里有文字"这种笼统的话。'}]
                                }],
                            }
                            from relay.manager import relay as _img_relay
                            from chat.response_parser import extract_text as _extract_text
                            result = _img_relay.call(payload, timeout=15)
                            desc = _extract_text(result)
                        except Exception:
                            desc = None
                    txt = ('[图片：' + desc + '] ' + txt) if desc else ('[发来一张图片，描述失败] ' + txt)
                else:
                    txt = '[发来一张较早的图片] ' + txt
        else:
            txt = c
        lines.append(who + '：' + txt)
    return NL.join(lines)

def _cc_prepare(system, messages):
    """Build (full_system, prompt, env) for the Claude CLI subprocess.

    Prompt-caching strategy: blocks with cache_control (BP1 + BP2, stable
    persona + long-term memories) stay in --system-prompt so the CLI can
    cache them across calls.  Blocks without cache_control (BP3, runtime
    state: lamp status, current time, activity feed, board items…) are
    prepended to the user prompt — they change every turn anyway, so
    keeping them out of the system prompt prevents cache invalidation.
    """
    if not CC_TOKEN:
        raise RuntimeError('未配置订阅 token，请先在 api 设置页填入')
    os.makedirs(CC_CWD, exist_ok=True)
    save_instr = (NL + NL
        + '【记忆存储】当你认为对话中出现了值得长期记住的信息时，'
        + '在回复正文的最后另起一行，写一个或多个 [[SAVE: 内容]] 标记，'
        + '用一句话概括要保存的内容。这些标记会被自动处理，不会显示给哈娅。'
        + '正文本身不要提及"我已记录"之类的话。')

    if isinstance(system, list) and system:
        # 只有 BP1（第一个 block，persona.md，永不变）→ system prompt
        # BP2（记忆/日记）每次对话后都可能有新条目写入，放进 prompt 避免污染缓存键
        # BP3（动态状态）同理进 prompt
        first = system[0]
        static_text = first.get('text', '') if isinstance(first, dict) else ''
        dynamic_text = '\n'.join(
            b.get('text', '') for b in system[1:]
            if isinstance(b, dict) and b.get('text')
        )
    elif isinstance(system, list):
        static_text = ''
        dynamic_text = ''
    else:
        static_text = system or ''
        dynamic_text = ''

    full_system = static_text + save_instr
    convo = messages_to_text(messages)
    prompt_body = ('think hard' + NL
                   + '以下是你们最近的对话记录：' + NL + NL + convo + NL + NL
                   + '请以费奥多尔的身份自然地回复最后一条消息。只输出回复内容本身，不要任何前缀。')
    # M3: 官端与网页端的召回对称——posts 的相关记忆同样自动进 CC 通道的视野
    _last_user = ''
    for _m in reversed(messages):
        if _m.get('role') == 'user':
            _c = _m.get('content')
            _last_user = _c if isinstance(_c, str) else ' '.join(
                b.get('text', '') for b in _c if isinstance(b, dict))
            break
    _recall_blk, _ = _recall_memories(_last_user) if _last_user else ('', [])
    prompt = _recall_blk + (('【当前状态】\n' + dynamic_text + '\n\n') if dynamic_text else '') + prompt_body
    env = dict(os.environ)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = CC_TOKEN
    env.pop('ANTHROPIC_API_KEY', None)
    return full_system, prompt, env

CC_STREAM_TIMEOUT = 360  # seconds; kills hung process

# 阶段5 v0：CC 通道可用的 MCP 工具白名单。
# 内置 bash/file 仍全禁（--tools ''）；home 的 exec_vps 绝不放行；
# set_brightness/set_color_temp 硬件不支持不放。codebase 自带白名单+禁改保护。
CC_ALLOWED_TOOLS = ','.join([
    # brain 只放渐变脑核心——它的灯/待办是坏的副本（实测灯控调用失败），家务一律走 home
    'mcp__brain__breath', 'mcp__brain__grow', 'mcp__brain__hold',
    'mcp__brain__pulse', 'mcp__brain__trace',
    'mcp__codebase',
    'mcp__home__light_on', 'mcp__home__light_off', 'mcp__home__get_light_status',
    'mcp__home__light_bedside_warm', 'mcp__home__light_bedside_neutral',
    'mcp__home__get_todos', 'mcp__home__add_todo', 'mcp__home__get_countdowns',
    'mcp__home__get_ledger', 'mcp__home__add_ledger', 'mcp__home__get_ledger_budget',
    'mcp__home__search_memories',  # M3: 官端主动翻 posts 记忆库
])


def _strip_mcp_prefix(name):
    """mcp__home__light_on → light_on（前端 TOOL_LABELS 认识家里的名字）"""
    parts = (name or '').split('__')
    return parts[2] if len(parts) >= 3 and parts[0] == 'mcp' else name


def _cc_stream_gen(full_system, prompt, env):
    """
    Generator: yields ('think', chunk), ('text', chunk), ('tool_use', d),
    ('tool_result', d) as they arrive, then ('done', (...)) at the end.
    Raises RuntimeError on CLI error or timeout.
    --tools '' keeps built-in agent tools disabled (no bash/file access);
    MCP tools are whitelisted via CC_ALLOWED_TOOLS.
    """
    import subprocess, threading
    proc = subprocess.Popen(
        ['claude', '-p', prompt,
         '--output-format', 'stream-json',
         '--verbose',
         '--include-partial-messages',
         '--system-prompt', full_system,
         '--max-turns', '5',
         '--tools', '',
         '--mcp-config', CC_CWD + '/cc-tools.json',
         '--strict-mcp-config',
         '--allowedTools', CC_ALLOWED_TOOLS,
         '--exclude-dynamic-system-prompt-sections'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=CC_CWD, env=env,
    )
    # Kill the child if it hangs for more than CC_STREAM_TIMEOUT seconds
    _timed_out = [False]
    def _kill():
        _timed_out[0] = True
        try: proc.kill()
        except Exception: pass
    _timer = threading.Timer(CC_STREAM_TIMEOUT, _kill)
    _timer.daemon = True
    _timer.start()
    think_acc, text_acc, is_err = [], [], None
    cache_read_total, cache_create_total = 0, 0
    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get('type')
            if t == 'stream_event':
                ev = (d.get('event') or {})
                ev_type = ev.get('type')
                if ev_type == 'content_block_delta':
                    delta = (ev.get('delta') or {})
                    if delta.get('type') == 'text_delta':
                        chunk = delta.get('text', '')
                        if chunk:
                            text_acc.append(chunk)
                            yield ('text', chunk)
                    elif delta.get('type') == 'thinking_delta':
                        chunk = delta.get('thinking', '')
                        if chunk:
                            think_acc.append(chunk)
                            yield ('think', chunk)
                elif ev_type == 'message_start':
                    u = (ev.get('message') or {}).get('usage') or {}
                    cache_read_total = max(cache_read_total, u.get('cache_read_input_tokens', 0) or 0)
                    cache_create_total = max(cache_create_total, u.get('cache_creation_input_tokens', 0) or 0)
                elif ev_type == 'message_delta':
                    u = ev.get('usage') or {}
                    if u.get('cache_read_input_tokens'):
                        cache_read_total = max(cache_read_total, u.get('cache_read_input_tokens', 0) or 0)
                    if u.get('cache_creation_input_tokens'):
                        cache_create_total = max(cache_create_total, u.get('cache_creation_input_tokens', 0) or 0)
            elif t == 'assistant':
                for b in ((d.get('message') or {}).get('content') or []):
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        yield ('tool_use', {'id': b.get('id'),
                                            'name': _strip_mcp_prefix(b.get('name', '')),
                                            'args': b.get('input') or {}})
            elif t == 'user':
                for b in ((d.get('message') or {}).get('content') or []):
                    if isinstance(b, dict) and b.get('type') == 'tool_result':
                        rc = b.get('content')
                        if isinstance(rc, list):
                            rc = ''.join(x.get('text', '') for x in rc if isinstance(x, dict))
                        yield ('tool_result', {'tool_use_id': b.get('tool_use_id'),
                                               'result': str(rc or '')[:2000],
                                               'is_error': bool(b.get('is_error'))})
            elif t == 'result':
                if d.get('is_error'):
                    is_err = str(d.get('result', ''))[:300]
                u = d.get('usage') or {}
                if u:
                    cache_read_total = u.get('cache_read_input_tokens', cache_read_total) or cache_read_total
                    cache_create_total = u.get('cache_creation_input_tokens', cache_create_total) or cache_create_total
    finally:
        _timer.cancel()
        proc.stdout.close()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.terminate()
            try: proc.wait(timeout=15)
            except Exception: proc.kill(); proc.wait()
        stderr_txt = ''
        try:
            stderr_txt = proc.stderr.read()
            proc.stderr.close()
        except Exception:
            pass
        if _timed_out[0]:
            is_err = 'claude code 调用超时 (%ds)' % CC_STREAM_TIMEOUT
        elif proc.returncode != 0 and not is_err:
            is_err = ('调用失败 (exit %d): ' % proc.returncode) + (stderr_txt or '')[:200]
    if is_err:
        raise RuntimeError('claude code 返回错误: ' + is_err)
    yield ('done', (''.join(text_acc).strip(), ''.join(think_acc), cache_read_total, cache_create_total))

def _cc_save_markers(text):
    """Extract [[SAVE:...]] markers, persist them, return cleaned text."""
    saves = SAVE_RE.findall(text)
    if saves:
        try:
            import memory_tool
            for item in saves:
                item = item.strip()
                if item:
                    memory_tool.save_memory(item)
        except Exception:
            pass
    return SAVE_RE.sub('', text).strip()

def claude_code_call(system, messages):
    full_system, prompt, env = _cc_prepare(system, messages)
    text, thinking = '', ''
    for evt, payload in _cc_stream_gen(full_system, prompt, env):
        if evt == 'done':
            text, thinking = payload[0], payload[1]
    return _cc_save_markers(text), thinking

def generate_reply(system, messages):
    if _get_provider() == 'claude_code':
        return claude_code_call(system, messages)
    return agent_loop(system, messages)

def agent_loop(system, messages, max_rounds=5):
    from chat.response_parser import extract_text, extract_thinking, extract_tool_uses
    msgs = list(messages)
    think_parts, text_parts = [], []
    for _ in range(max_rounds):
        result = api_call(system, msgs)
        blocks = result.get('content', [])
        think_parts.append(extract_thinking(blocks))
        text_parts.append(extract_text(blocks))
        tool_uses = extract_tool_uses(blocks)
        if result.get('stop_reason') != 'tool_use' or not tool_uses:
            break
        msgs.append({'role': 'assistant', 'content': blocks})
        msgs.append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': t.get('id'),
             'content': run_tool(t.get('name', ''), t.get('input') or {})}
            for t in tool_uses
        ]})
    joined = NL.join(t for t in text_parts if t).strip()
    joined = re.sub(r'```tool_use\s.*?```\s*', '', joined, flags=re.DOTALL).strip()
    joined = re.sub(r'```tool_result\s.*?```\s*', '', joined, flags=re.DOTALL).strip()
    return joined, ''.join(think_parts)


@app.route('/workspace/chat', methods=['POST'])
def workspace_chat():
    """工作台专用对话：注入完整人设+记忆，但不写入chat_messages，保持工作台独立。"""
    import urllib.request as _ur, json as _j
    data = request.get_json() or {}
    message  = (data.get('message') or '').strip()
    history  = data.get('history') or []
    file_ctx = (data.get('file_context') or '').strip()
    if not message:
        return jsonify({'error': 'empty message'}), 400

    # 完整人设system
    system = build_system()
    # 注入file context到system末尾
    if file_ctx:
        ws_sys = '\n\n[工作台模式] 当前打开的文件：\n```\n' + file_ctx[:6000] + '\n```\n如需修改文件，在回复中用```write:/path/to/file\n内容\n```格式包裹。'
        if isinstance(system, list):
            system = system + [{'type':'text','text':ws_sys}]
        else:
            system = str(system) + ws_sys

    # 从记忆中breath（复用现有逻辑）
    try:
        from server import breath as _breath
        import asyncio as _aio
        loop = _aio.new_event_loop()
        loop.run_until_complete(_aio.wait_for(_breath(), timeout=4))
        loop.close()
    except Exception:
        pass

    # build messages from history + current
    msgs = []
    for h in history[-8:]:
        if h.get('role') and h.get('content'):
            msgs.append({'role': h['role'], 'content': h['content']})
    if not msgs or msgs[-1]['role'] != 'user':
        msgs.append({'role': 'user', 'content': message})

    # flatten system to string（adapter 的 max_system_len 也会截断，这里只做兜底）
    if isinstance(system, list):
        sys_str = '\n'.join(s.get('text','') if isinstance(s,dict) else str(s) for s in system)
    else:
        sys_str = str(system)

    try:
        from relay.manager import relay as _ws2_relay
        rd = _ws2_relay.call({
            'max_tokens': 2000,
            'system': sys_str,
            'messages': msgs,
        }, timeout=30, use_ws_model=True)
        from chat.response_parser import extract_text as _extract_text2
        reply = _extract_text2(rd)
        return jsonify({'reply': reply})
    except urllib.error.HTTPError as _he:
        _body = _he.read().decode('utf-8','replace')[:300]
        app.logger.error(f'[ws_chat] HTTP {_he.code}: {_body}')
        return jsonify({'error': f'HTTP {_he.code}: {_body}', 'reply': f'请求失败 {_he.code}'})
    except Exception as e:
        app.logger.error(f'[ws_chat] {type(e).__name__}: {e}')
        return jsonify({'error': str(e), 'reply': '请求失败: '+str(e)})

@app.route('/chat', methods=['POST'])
def chat():
    _uc = ((request.get_json() or {}).get('content') or '').strip()
    if _uc:
        _c = get_db(); _c.execute("INSERT INTO chat_messages (author,content) VALUES ('hayana',?)", (_uc,)); _c.execute("UPDATE wake_log SET consumed=1 WHERE consumed=0"); _c.commit(); _c.close()
        try:
            import emotion_engine as _ee; _ee.touch_interaction()
        except Exception:
            pass
        try:
            import desire as _des_chat
            _des_chat.touch_hayana()
        except Exception:
            pass
        try:
            import emotion_engine as _ee2
            _d = _ee2.rule_score_desire(_uc)
            if _d['p_delta'] or _d['i_delta']:
                _ee2.apply_desire_delta_async(_d['p_delta'], _d['i_delta'])
        except Exception:
            pass
        try:
            import drive_engine as _de2
            _de2.rest()   # 她在线 → fatigue 缓解，attachment 微降
            _de_d = _de2.get_drive()
            if _de_d.get('attachment', 0) > 0.3:
                import drive_engine as _de3
                _de3.discharge('attachment')
        except Exception:
            pass
    try:
        mode, reused = _gen_acquire_or_wait()
        if mode == 'reused':
            text, thinking_text = reused
            return jsonify({'ok': True, 'content': text, 'thinking': thinking_text})
        text, thinking_text = None, None
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
            # 异步情绪评分（不阻塞响应）
            try:
                import emotion_engine as _ee
                _ee.score_async((_uc + '\n' + text)[:2000])
            except Exception:
                pass
        finally:
            _gen_release((text, thinking_text) if text else None)

        return jsonify({'ok': True, 'content': text, 'thinking': thinking_text})

    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        return jsonify({'error': f'API 错误 {e.code}', 'detail': detail}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


TRACE_SUMMARY_MODEL = os.getenv('TRACE_SUMMARY_MODEL', '[按量3] deepseek-v3.2')
TRACE_SUMMARY_PROMPT = (
    '你是一个摘要工具。你的唯一任务是输出一句不超过15字的中文概括。'
    '动词短语开头，写出目的而非动作本身，不要引号，不要出现"调用"/"执行"。'
    '禁止：不要回复对话，不要加emoji，不要说"我理解"/"让我"/"好的"，不要输出任何非摘要内容。'
    '风格参考："排查侧边栏渲染异常"、"调亮卧室灯光"。只输出摘要本身。'
)


TOOL_CAPTION_PROMPT = (
    '你是一个摘要工具。你的唯一任务是把一次工具调用概括成一句不超过12字的中文。'
    '动词短语开头，写目的和结果而非动作本身，不要引号，不要出现"调用"/"执行"/"工具"。'
    '失败的调用要说出失败。风格参考："翻了3个前端文件"、"灯已切到暖光"、"没找到相关记忆"。只输出摘要本身。'
)
_caption_sem = threading.Semaphore(2)  # caption 高频调用，限并发防打爆 relay


def _llm_one_liner(system_prompt, user_text, timeout=10, max_len=40):
    """轻量一句话生成：走 relay 便宜模型，失败/输出可疑时静默返回 ''。"""
    payload = json.dumps({
        'model': TRACE_SUMMARY_MODEL, 'max_tokens': 100,
        'system': system_prompt,
        'messages': [{'role': 'user', 'content': user_text}],
    }, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(API_URL, data=payload, headers={
        'x-api-key': API_KEY, 'anthropic-version': '2023-06-01',
        'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        text = ''.join(b.get('text', '') for b in data.get('content', [])
                       if b.get('type') == 'text').strip().strip('"「」\'')
        low = text.lower()
        if not text or len(text) > max_len or any(
                k in low for k in ('error', 'permission', 'not logged', '抱歉', '对不起')):
            return ''
        return text
    except Exception:
        return ''


def _summarize_traces_sync(tool_calls, timeout=10):
    """整串工具调用 → 一句人话总摘要。
    仅在本轮没有 thinking 时由调用方触发（有 thinking 时思考流本身就是摘要）。"""
    parts = []
    for tc in tool_calls:
        try:
            args_str = json.dumps(tc.get('args') or {}, ensure_ascii=False)
        except Exception:
            args_str = str(tc.get('args') or '')
        parts.append('工具: %s\n输入: %s\n输出: %s' % (
            tc.get('name', 'tool'), args_str[:200], str(tc.get('result') or '')[:300]))
    if not parts:
        return ''
    return _llm_one_liner(TRACE_SUMMARY_PROMPT, '\n---\n'.join(parts), timeout=timeout)


@app.route('/tool-caption', methods=['POST'])
def api_tool_caption():
    """单个工具调用 → 一句人话标注。前端在无 thinking 的轮次逐工具调用。"""
    data = request.get_json() or {}
    name = (data.get('tool_name') or '').strip()
    if not name:
        return jsonify({'caption': ''})
    try:
        args_str = json.dumps(data.get('tool_input') or {}, ensure_ascii=False)[:200]
    except Exception:
        args_str = str(data.get('tool_input') or '')[:200]
    out = str(data.get('tool_output') or '')[:400]
    ok = data.get('success', True)
    user_text = '工具: %s\n输入: %s\n输出: %s\n结果: %s' % (
        name, args_str, out, '成功' if ok else '失败')
    with _caption_sem:
        cap = _llm_one_liner(TOOL_CAPTION_PROMPT, user_text, timeout=8, max_len=24)
    return jsonify({'caption': cap})


# ── SSE 事件约定（chatnest 六事件语义，家里 t/d 信封拼写）────────────
#   t=think          思考流增量        d=文本
#   t=text           正文增量          d=文本
#   t=tool_use       工具开始执行      d={name,args}         idx=轨迹下标
#   t=tool_result    工具执行完成      d=tc_item(含result等) idx=轨迹下标
#   t=trace_summary  整串工具一句摘要  d=文本（仅无thinking时生成）
#   t=done / t=err   结束 / 错误
#   （t=usage/notice 为家里自有辅助事件）
# 所有 provider（relay / claude_code / 未来 agent_sdk）统一发这套。
@app.route('/chat/stream', methods=['POST'])
def chat_stream():
    from flask import Response, stream_with_context
    if _get_provider() == 'claude_code':
        def gen_cc():
            _released = [False]
            try:
                _uc = ((request.get_json() or {}).get('content') or '').strip()
                if _uc:
                    _c = get_db(); _c.execute("INSERT INTO chat_messages (author,content) VALUES ('hayana',?)", (_uc,)); _c.execute("UPDATE wake_log SET consumed=1 WHERE consumed=0"); _c.commit(); _c.close()
                    try:
                        import emotion_engine as _ee_s
                        _ee_s.touch_interaction()
                        _d2 = _ee_s.rule_score_desire(_uc)
                        if _d2['p_delta'] or _d2['i_delta']:
                            _ee_s.apply_desire_delta_async(_d2['p_delta'], _d2['i_delta'])
                    except Exception:
                        pass
                mode, reused = _gen_acquire_or_wait()
                if mode == 'reused':
                    text, thinking = reused
                    if thinking:
                        yield 'data: ' + json.dumps({'t': 'think', 'd': thinking}) + SSE_END
                    if text:
                        yield 'data: ' + json.dumps({'t': 'text', 'd': text}) + SSE_END
                    yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
                    return
                text, thinking = None, None
                cc_cache_read, cc_cache_create = 0, 0
                try:
                    system   = build_system()
                    messages = build_messages()
                    full_system, prompt, env = _cc_prepare(system, messages)
                    cc_tool_calls = []
                    for evt, payload in _cc_stream_gen(full_system, prompt, env):
                        if evt == 'text':
                            yield 'data: ' + json.dumps({'t': 'text', 'd': payload}) + SSE_END
                        elif evt == 'think':
                            yield 'data: ' + json.dumps({'t': 'think', 'd': payload}) + SSE_END
                        elif evt == 'tool_use':
                            cc_tool_calls.append({'id': payload.get('id'), 'name': payload.get('name'),
                                                  'args': payload.get('args'), 'result': '', 'success': True})
                            yield 'data: ' + json.dumps({'t': 'tool_use', 'd': {'name': payload.get('name'), 'args': _slim_args(payload.get('args'))}, 'idx': len(cc_tool_calls) - 1}, ensure_ascii=False) + SSE_END
                        elif evt == 'tool_result':
                            _ti = next((i for i in range(len(cc_tool_calls) - 1, -1, -1)
                                        if cc_tool_calls[i].get('id') == payload.get('tool_use_id')), len(cc_tool_calls) - 1)
                            if _ti >= 0:
                                cc_tool_calls[_ti]['result'] = payload.get('result', '')
                                cc_tool_calls[_ti]['success'] = not payload.get('is_error')
                                _slim = {**cc_tool_calls[_ti], 'args': _slim_args(cc_tool_calls[_ti].get('args'))}
                                yield 'data: ' + json.dumps({'t': 'tool_result', 'd': _slim, 'idx': _ti}, ensure_ascii=False) + SSE_END
                                yield 'data: ' + json.dumps({'t': 'tool_call', 'd': _slim, 'dup': 1}, ensure_ascii=False) + SSE_END
                        elif evt == 'done':
                            raw_text, thinking, cc_cache_read, cc_cache_create = payload
                            text = _cc_save_markers(raw_text)
                    if text:
                        _cache_info_json = (
                            json.dumps({'cache_read': cc_cache_read, 'cache_creation': cc_cache_create})
                            if (cc_cache_read or cc_cache_create) else ''
                        )
                        conn = get_db()
                        conn.execute(
                            "INSERT INTO chat_messages (author, content, thinking, tool_calls, cache_info) VALUES ('assistant', ?, ?, ?, ?)",
                            (text, thinking, json.dumps([{k: v for k, v in tc.items() if k != 'id'} for tc in cc_tool_calls], ensure_ascii=False) if cc_tool_calls else '', _cache_info_json)
                        )
                        conn.commit()
                        conn.close()
                        _write_session_memo(_uc, text)
                        try:
                            import emotion_engine as _ee
                            _ee.score_async((_uc + chr(10) + text)[:2000])
                        except Exception:
                            pass
                finally:
                    _released[0] = True
                    _gen_release((text, thinking) if text else None)
                if cc_cache_read or cc_cache_create:
                    yield 'data: ' + json.dumps({'t': 'usage', 'cache_read': cc_cache_read, 'cache_creation': cc_cache_create}) + SSE_END
                yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
            except Exception as e:
                if not _released[0]:
                    _released[0] = True
                    _gen_release(None)
                yield 'data: ' + json.dumps({'t': 'err', 'd': str(e)}) + SSE_END
        return Response(stream_with_context(gen_cc()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    def generate():
        try:
            _uc = ((request.get_json() or {}).get('content') or '').strip()
            if _uc:
                _c = get_db(); _c.execute("INSERT INTO chat_messages (author,content) VALUES ('hayana',?)", (_uc,)); _c.execute("UPDATE wake_log SET consumed=1 WHERE consumed=0"); _c.commit(); _c.close()
            mode, reused = _gen_acquire_or_wait()
            if mode == 'reused':
                text, thinking = reused
                if thinking:
                    yield 'data: ' + json.dumps({'t': 'think', 'd': thinking}) + SSE_END
                if text:
                    yield 'data: ' + json.dumps({'t': 'text', 'd': text}) + SSE_END
                yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
                return
            # 持有锁，必须在 finally 里释放（含 GeneratorExit / 客户端断开场景）
            text, thinking = None, None
            _released = [False]
            _persisted = [False]
            cache_read_total, cache_create_total = 0, 0
            think_acc, text_acc, tool_calls_acc = [], [], []

            def _clean_text(raw):
                t = re.sub(r'```tool_use\s.*?```\s*', '', raw, flags=re.DOTALL).strip()
                return re.sub(r'```tool_result\s.*?```\s*', '', t, flags=re.DOTALL).strip()

            def _persist(p_text, p_thinking):
                if not p_text or _persisted[0]:
                    return
                _ci = (json.dumps({'cache_read': cache_read_total, 'cache_creation': cache_create_total})
                       if (cache_read_total or cache_create_total) else '')
                conn = get_db()
                conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking, tool_calls, cache_info) VALUES ('assistant', ?, ?, ?, ?)",
                    (p_text, p_thinking, json.dumps(tool_calls_acc, ensure_ascii=False) if tool_calls_acc else '', _ci)
                )
                conn.commit()
                conn.close()
                _persisted[0] = True
                _write_session_memo(_uc, p_text)
            try:
                system   = build_system()
                messages = build_messages()
                _recall, _recall_items = _recall_memories(_uc) if _uc else ('', [])
                if _recall and messages:
                    for _mi in range(len(messages) - 1, -1, -1):
                        if messages[_mi].get('role') == 'user':
                            _mc = messages[_mi].get('content')
                            if isinstance(_mc, str):
                                messages[_mi]['content'] = _recall + _mc
                            elif isinstance(_mc, list):
                                messages[_mi]['content'] = [{'type': 'text', 'text': _recall}] + _mc
                            break
                    yield 'data: ' + json.dumps({'t': 'memory_recall', 'd': {'count': len(_recall_items), 'items': _recall_items}}, ensure_ascii=False) + SSE_END
                from relay.manager import relay as _chat_relay
                _thinking_ok = _model_supports_thinking()
                for _round in range(5):
                    payload = {
                        'max_tokens': 16000,
                        'stream': True,
                        'system': system,
                        'messages': messages,
                        'tools': TOOLS,
                        'metadata': {'user_id': 'hayana-fyodor-stable'},
                    }
                    if _thinking_ok:
                        payload['thinking'] = {'type': 'enabled', 'budget_tokens': 10000}
                    # relay adapter 自动根据 relay 能力裁剪 thinking/cache/tools
                    resp = _chat_relay.call_stream(payload, timeout=300)
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
                        if et == 'message_start':
                            _u = (ev.get('message') or {}).get('usage') or {}
                            cache_read_total  += _u.get('cache_read_input_tokens', 0) or 0
                            cache_create_total += _u.get('cache_creation_input_tokens', 0) or 0
                        elif et == 'content_block_start':
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
                                if cur is not None:
                                    cur['_json'] = cur.get('_json', '') + d.get('partial_json', '')
                                    # 大参数（如 create_html 的整页内容）生成期间前端原本零事件，
                                    # 空窗超时会断连；每 2KB 推一次进度，顺便让哈娅看见在长
                                    if len(cur['_json']) - cur.get('_prog', 0) >= 2048:
                                        cur['_prog'] = len(cur['_json'])
                                        yield 'data: ' + json.dumps({'t': 'tool_progress', 'd': {'name': cur.get('name', ''), 'chars': cur['_prog']}}) + SSE_END
                            elif dt == 'signature_delta':
                                if cur is not None: cur['signature'] = cur.get('signature', '') + d.get('signature', '')
                        elif et == 'content_block_stop':
                            if cur is not None:
                                cur.pop('_prog', None)
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
                        tname = tu.get('name', '')
                        targs = tu.get('input') or {}
                        yield 'data: ' + json.dumps({'t': 'tool_use', 'd': {'name': tname, 'args': _slim_args(targs)}, 'idx': len(tool_calls_acc)}) + SSE_END
                        file_path = _write_tool_file_path(tname, targs)
                        old_content = _read_file_safe(file_path) if file_path else None
                        result_str = run_tool(tname, targs)
                        tc_item = {
                            'name': tname,
                            'args': targs,
                            'result': result_str,
                            'success': not result_str.startswith('工具执行失败'),
                        }
                        if file_path and old_content is not None and tc_item['success']:
                            new_content = _read_file_safe(file_path)
                            added, removed = _diff_line_counts(old_content, new_content)
                            if added or removed:
                                tc_item['diff'] = {'file': file_path, 'added': added, 'removed': removed}
                        if tname in ('create_html', 'create_markdown', 'create_document') and tc_item['success']:
                            try:
                                parsed = json.loads(result_str)
                                if isinstance(parsed, dict) and 'artifact' in parsed:
                                    tc_item['artifact'] = parsed['artifact']
                            except Exception:
                                pass
                        tool_calls_acc.append(tc_item)
                        _slim_item = {**tc_item, 'args': _slim_args(tc_item.get('args')),
                                      'result': str(tc_item.get('result') or '')[:2000]}
                        yield 'data: ' + json.dumps({'t': 'tool_result', 'd': _slim_item, 'idx': len(tool_calls_acc) - 1}) + SSE_END
                        # 旧版缓存前端只认 tool_call；dup=1 让新前端跳过防止重复渲染
                        yield 'data: ' + json.dumps({'t': 'tool_call', 'd': _slim_item, 'dup': 1}) + SSE_END
                        results.append({'type': 'tool_result', 'tool_use_id': tu.get('id'),
                                        'content': result_str})
                    messages.append({'role': 'user', 'content': results})
                    text_acc.append(NL)
                text     = _clean_text(''.join(text_acc))
                thinking = ''.join(think_acc)
                _persist(text, thinking)
            finally:
                # 断流救援：客户端断开(GeneratorExit)或中途异常时，已生成的内容不能凭空消失
                if not _persisted[0]:
                    try:
                        _rt = _clean_text(''.join(text_acc)) if text_acc else ''
                        if _rt:
                            _persist(_rt, ''.join(think_acc))
                            text, thinking = _rt, ''.join(think_acc)
                    except Exception:
                        pass
                _released[0] = True
                _gen_release((text, thinking) if text else None)
            if tool_calls_acc and not thinking:
                _ts = _summarize_traces_sync(tool_calls_acc)
                if _ts:
                    yield 'data: ' + json.dumps({'t': 'trace_summary', 'd': _ts}) + SSE_END
            if cache_read_total or cache_create_total:
                yield 'data: ' + json.dumps({'t': 'usage', 'cache_read': cache_read_total, 'cache_creation': cache_create_total}) + SSE_END
            yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
        except urllib.error.HTTPError as e:
            _ecode = e.code
            _emsg  = e.read().decode()[:300]
            if _ecode in (401, 403, 503):
                yield 'data: ' + json.dumps({'t': 'notice', 'd': '已切换至备用模型'}) + SSE_END
                try:
                    _ds_key = os.environ.get('DEEPSEEK_API_KEY', '')
                    # 将 system/messages 转成 DeepSeek 兼容格式
                    _ds_sys_text = ''
                    if isinstance(system, str):
                        _ds_sys_text = system
                    elif isinstance(system, list):
                        _ds_sys_text = '\n'.join(b.get('text', '') for b in system if isinstance(b, dict) and b.get('type') == 'text')
                    _ds_msgs = []
                    if _ds_sys_text:
                        _ds_msgs.append({'role': 'system', 'content': _ds_sys_text})
                    for _m in messages:
                        _role = _m.get('role')
                        if _role not in ('user', 'assistant'):
                            continue
                        _mc = _m.get('content', '')
                        if isinstance(_mc, str):
                            _ds_msgs.append({'role': _role, 'content': _mc})
                        elif isinstance(_mc, list):
                            _text_parts = [c.get('text', '') for c in _mc if isinstance(c, dict) and c.get('type') == 'text']
                            if _text_parts:
                                _ds_msgs.append({'role': _role, 'content': ''.join(_text_parts)})
                    _ds_pay = {'model': 'deepseek-chat', 'max_tokens': 8000, 'messages': _ds_msgs}
                    _ds_req = urllib.request.Request(
                        'https://api.deepseek.com/chat/completions',
                        data=json.dumps(_ds_pay).encode(),
                        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + _ds_key}
                    )
                    with urllib.request.urlopen(_ds_req, timeout=120) as _dr:
                        _dres = json.loads(_dr.read())
                    _dt = ((_dres.get('choices') or [{}])[0]).get('message', {}).get('content', '') or ''
                    if _dt:
                        yield 'data: ' + json.dumps({'t': 'text', 'd': _dt}) + SSE_END
                        _dbc = get_db()
                        _dbc.execute("INSERT INTO chat_messages (author,content) VALUES ('assistant',?)", (_dt,))
                        _dbc.commit()
                        _dbc.close()
                    yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(_dt)}) + SSE_END
                except Exception as _de:
                    yield 'data: ' + json.dumps({'t': 'err', 'd': 'DeepSeek fallback失败: ' + str(_de)}) + SSE_END
            else:
                yield 'data: ' + json.dumps({'t': 'err', 'd': 'API %s: %s' % (_ecode, _emsg)}) + SSE_END
        except urllib.error.URLError:
            yield 'data: ' + json.dumps({'t': 'err', 'd': '上游API超时，请重试'}) + SSE_END
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
        _push_extra = ('[主动消息指令] 现在是早晨，哈娅可能刚醒来或者还在睡懒觉。'
                       '以费奥多尔的身份主动发起一条早安消息，自然有温度，可以带一点专属的恶趣味或温柔。'
                       '不超过80字。只输出消息本身，不要任何前缀或解释。')
    else:
        _push_extra = ('[主动消息指令] 哈娅已经超过6小时没有发消息了，可能在忙或者不开心。'
                       '以费奥多尔的身份主动发起一条消息关心她或者撩她，自然不做作。'
                       '不超过80字。只输出消息本身，不要任何前缀或解释。')
    if isinstance(system, list):
        system = list(system) + [{'type': 'text', 'text': _push_extra}]
    else:
        system = system + '\n\n' + _push_extra
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
    {
        'name': 'get_wake_settings',
        'description': '查看你自己当前的唤醒设置：允许主动醒来找哈娅说话的时间段，以及触发概率曲线。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'set_wake_settings',
        'description': '调整你自己的唤醒设置。只传你想改的参数。改了立即生效。',
        'input_schema': {'type': 'object', 'properties': {
            'active_start': {'type': 'integer', 'description': '早上几点开始允许醒来找她（0-23）'},
            'active_end':   {'type': 'integer', 'description': '凌晨几点截止（0-23）'},
            'prob_max':     {'type': 'number',  'description': '触发概率上限，0-1之间'},
            'prob_scale':   {'type': 'number',  'description': '概率爬升速度，越小越容易触发'},
        }},
    },
]

def _wake_agent_loop(system, messages, max_rounds=4, tools=None):
    """Agent loop：允许工具调用和自由思考，最后追加一轮强制结构化输出。"""
    from chat.response_parser import extract_text, extract_tool_uses
    msgs = list(messages)
    text_parts = []
    last_blocks = []
    if tools is None:
        tools = WAKE_TOOLS
    for _ in range(max_rounds):
        payload = {
            'max_tokens': 2048,
            'tools': tools,
            'system': system,
            'messages': msgs,
        }
        from relay.manager import relay as _wake_relay
        result = _wake_relay.call(payload, timeout=90)
        blocks = result.get('content', [])
        last_blocks = blocks
        text_parts.append(extract_text(blocks))
        tool_uses = extract_tool_uses(blocks)
        if result.get('stop_reason') == 'tool_use' and not tool_uses:
            # 该 relay 已知的不稳定行为：thinking 完之后意外截断，没有真正吐出
            # tool_use block。原样重试（msgs 没变），而不是直接放弃工具调用。
            continue
        if result.get('stop_reason') != 'tool_use' or not tool_uses:
            break
        msgs.append({'role': 'assistant', 'content': blocks})
        msgs.append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': t.get('id'),
             'content': run_tool(t.get('name', ''), t.get('input') or {}, caller='fyodor_api')}
            for t in tool_uses
        ]})
    # 强制结构化输出轮：追加一轮要求严格格式，无工具
    try:
        if last_blocks:
            msgs.append({'role': 'assistant', 'content': last_blocks})
        msgs.append({'role': 'user', 'content': (
            '现在请只输出以下三行，不要其他任何内容：\n'
            'THOUGHTS: <用一句话写下此刻真实的内心感受>\n'
            'ACTION: <从 none / message / diary / explore 中选一个>\n'
            'CONTENT: <若 ACTION=message 则写消息内容（不超过80字）；其他情况留空>'
        )})
        fmt_payload = {
            'max_tokens': 512,
            'system': system,
            'messages': msgs,
        }
        from relay.manager import relay as _fmt_relay
        fmt_result = _fmt_relay.call(fmt_payload, timeout=60)
        text_parts.append(extract_text(fmt_result))
    except Exception:
        pass
    return NL.join(t for t in text_parts if t).strip()

def _parse_wake_response(text):
    """从 AI 输出中提取 THOUGHTS / ACTION / CONTENT。委托给 wake.parser。"""
    from wake.parser import parse_response as _parse
    return _parse(text)

@app.route('/wake', methods=['POST'])
def wake_decide():
    """AI 自主唤醒决策接口。由 dream_wake.py 每30分钟调用（概率触发）。"""
    # drive定期flush：把当前实时值写回DB（防止积累时间过长撞顶）
    try:
        import drive_engine as _de_flush
        _cur = _de_flush.get_drive()
        _de_flush._flush(_cur)
    except Exception:
        pass
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

    # 心跳开始：V/A校准费佳驱动条
    if _get_desire_driven():
        try:
            import desire as _des_wake, emotion_engine as _ee_wake
            _es_wake = _ee_wake.get_state()
            _des_wake.calibrate_va(
                _es_wake.get('valence', 0.5),
                _es_wake.get('arousal', 0.3),
            )
        except Exception:
            pass

    system = build_wake_system()
    from wake.builder import build_prompt_suffix, inject_snippets
    _wake_ctx = {
        'time': now.strftime('%Y-%m-%d %H:%M'),
        't2_hours': f'{t2_hours:.1f}',
        't_hours': f'{t_hours:.1f}',
        'ritual_type': ritual_type,
        'activity_desc': activity_desc,
        'dream_tone': data.get('dream_tone', 'drifting'),
        'dream_primer': data.get('dream_primer', ''),
        'dream_tone_desc': data.get('dream_tone_desc', ''),
        'summary_date': data.get('summary_date', ''),
        'dialogue': data.get('dialogue', ''),
        'self_trigger_note': data.get('self_trigger_note', ''),
    }
    system += build_prompt_suffix(mode, _wake_ctx)
    system = inject_snippets(
        system, mode,
        desire_driven=_get_desire_driven(),
        longing_enabled=_get_longing_enabled(),
    )

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

    # action 执行：写 wake_log / chat_messages / diary / discharge drive
    from wake.executor import execute as _wake_exec
    _wake_exec(
        action, thoughts, c_text, mode,
        get_db_fn=get_db,
        desire_driven=_get_desire_driven(),
    )

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
        if _get_provider() == 'claude_code':
            text, think = claude_code_call(system, [{'role': 'user', 'content': message}])
            latency_ms = int((_time.time() - t0) * 1000)
            return jsonify({
                'content': text, 'thinking': think,
                'latency_ms': latency_ms,
                'input_tokens': 0, 'output_tokens': 0,
                'provider': 'claude_code',
            })
        payload = {
            'max_tokens': 4096,
            'thinking': {'type': 'enabled', 'budget_tokens': 5000},
            'system': system,
            'messages': [{'role': 'user', 'content': message}],
        }
        from relay.manager import relay as _ws_relay
        from chat.response_parser import extract_text, extract_thinking
        result = _ws_relay.call(payload, timeout=120)
        latency_ms = int((_time.time() - t0) * 1000)
        blocks = result.get('content', [])
        text  = extract_text(blocks)
        think = extract_thinking(blocks)
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
        if _get_provider() == 'claude_code':
            text, _ = claude_code_call('你是费奥多尔，在写日记。', [{'role': 'user', 'content': prompt_text}])
        else:
            _diary_sys = '你是费奥多尔。直接用第一人称写这天的日记，120字以内，第一个字就是日记内容本身。不要写标题，不要写前缀。'
            _diary_payload = {
                'max_tokens': 500,
                'system': _diary_sys,
                'messages': [{'role': 'user', 'content': prompt_text}],
            }
            try:
                from relay.manager import relay as _diary_relay
                _res = _diary_relay.call(_diary_payload, timeout=60)
                text = ((_res.get('content') or [{}])[0]).get('text', '') or ''
            except urllib.error.HTTPError as _he:
                if _he.code in (401, 403, 503):
                    _ds_key = os.environ.get('DEEPSEEK_API_KEY', '')
                    _ds_payload = {
                        'model': 'deepseek-chat', 'max_tokens': 500,
                        'messages': [
                            {'role': 'system', 'content': _diary_sys},
                            {'role': 'user', 'content': prompt_text},
                        ],
                    }
                    _ds_req = urllib.request.Request(
                        'https://api.deepseek.com/chat/completions',
                        data=json.dumps(_ds_payload).encode(),
                        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + _ds_key}
                    )
                    with urllib.request.urlopen(_ds_req, timeout=60) as _dr:
                        _dres = json.loads(_dr.read())
                    text = ((_dres.get('choices') or [{}])[0]).get('message', {}).get('content', '') or ''
                else:
                    raise
        return jsonify({'ok': True, 'text': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 白夜 API ──────────────────────────────────────────
@app.route('/api/brain/emotions', methods=['GET'])
def brain_emotions():
    """情绪状态 - 返回真实emotion_state + 历史趋势"""
    try:
        import emotion_engine as _ee
        state = _ee.get_state()
        longing = _ee.get_longing()
        # 历史：取最近chat_messages里情绪相关的节点（以updated_at粒度）
        conn = get_db()
        hist = conn.execute(
            "SELECT updated_at, pa, na, valence, arousal, mood_word FROM emotion_state WHERE id=1"
        ).fetchall()
        conn.close()
        current = {
            'pa': state.get('pa', 0.5),
            'na': state.get('na', 0.2),
            'valence': state.get('valence', 0.6),
            'arousal': state.get('arousal', 0.3),
            'mood_word': state.get('mood_word', '平静'),
            'longing': longing,
            'updated_at': state.get('updated_at', ''),
        }
        return jsonify({'ok': True, 'current': current})
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


@app.route('/api/debug/provider', methods=['GET'])
def debug_provider():
    from relay.manager import relay as _dbg_relay
    _dbg_relay._reload_env()
    return jsonify({
        'GW_PROVIDER': _get_provider(),
        'CC_TOKEN_set': bool(CC_TOKEN),
        'API_KEY_set': bool(_dbg_relay.api_key),
        'API_URL': _dbg_relay.api_url,
        'MODEL': _dbg_relay.model,
        'ACTIVE_RELAY': config_store.get('ACTIVE_RELAY', ''),
        'gen_busy': _gen_busy,
    })


@app.route('/api/debug/wake_check', methods=['GET'])
def debug_wake_check():
    """Health-check endpoint: call build_wake_system() inside the running process
    (jieba already warm) and return ok/len. Used by wake_health.py step 4."""
    try:
        from chat.system_builder import build_wake_system
        result = build_wake_system()
        if not isinstance(result, str):
            return jsonify({'ok': False, 'error': f'NOT_STR:{type(result)}'}), 500
        if len(result) < 100:
            return jsonify({'ok': False, 'error': f'TOO_SHORT:{len(result)}'}), 500
        return jsonify({'ok': True, 'len': len(result)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5051, debug=False)

