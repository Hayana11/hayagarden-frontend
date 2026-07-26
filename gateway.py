import os, re, sqlite3, json, base64, mimetypes, datetime, threading, time, sys as _sys, random, shutil
if '/opt/frontend' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend')
if '/opt/frontend' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend')
if '/opt/frontend/tools' not in _sys.path:
    _sys.path.insert(0, '/opt/frontend/tools')
from flask import Flask, request, jsonify
import urllib.request, urllib.error, urllib.parse
from codebase.client import CODEBASE_TOOLS, CODEBASE_READ_TOOLS, run_codebase_tool
from tools import workspace_agent
from tools import workspace_jobs
from tools import workspace_apps
from tools import ombre_adapter
from tools.workspace_apps import WorkspaceAppError, verified_proxy_upstream, proxy_target

app = Flask(__name__)
DB_PATH    = '/opt/frontend/memories.db'
_tool_ctx = threading.local()

def _warmup_ombre_brain():
    """Warm Ombre through the shared adapter without blocking gateway import."""
    ombre_adapter.warmup_async()


_warmup_ombre_brain()


def _workspace_job_event_hook(event):
    """Job 完成：入队 SSE/轮询事件，并写入 chat_messages（不触发新生成）。"""
    workspace_jobs.queue_event(event)
    if event.get('type') != 'job_finished':
        return
    try:
        meta = event.get('meta') or {}
        tc = [{
            'name': 'ws_job',
            'args': {'action': 'status', 'id': meta.get('job_id')},
            'result': json.dumps({
                'ok': True,
                'job': meta,
                'log_tail': event.get('log_tail', ''),
            }, ensure_ascii=False),
            'success': meta.get('status') == 'succeeded',
            'job': meta,
        }]
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT INTO chat_messages (author, content, tool_calls) VALUES ('assistant', ?, ?)",
            (event.get('content', ''), json.dumps(tc, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _workspace_job_sse_payloads():
    for ev in workspace_jobs.drain_pending_events():
        if ev.get('type') != 'job_finished':
            continue
        meta = ev.get('meta') or {}
        yield {
            't': 'workspace_job',
            'd': {
                'job_id': meta.get('job_id'),
                'status': meta.get('status'),
                'preview': ev.get('preview', ''),
                'log_tail': ev.get('log_tail', ''),
                'exit_code': meta.get('exit_code'),
            },
        }
        preview = ev.get('preview') or meta.get('job_id') or '后台任务'
        yield {'t': 'notice', 'd': f'后台任务完成：{preview}', 'dup': 1}


def _init_workspace_jobs():
    workspace_jobs.set_event_hook(_workspace_job_event_hook)
    workspace_jobs.start_sweep_thread()


def _init_workspace_apps():
    try:
        results = workspace_apps.autostart_apps()
        if results:
            print(f"[workspace_apps] autostart: {results}", flush=True)
    except Exception as exc:
        print(f"[workspace_apps] autostart failed: {exc}", flush=True)


_init_workspace_jobs()
_init_workspace_apps()
STATIC_DIR = '/opt/frontend/static'

import config_store
import attachment_store
import tool_drawers
import group_chat_store
import codex_app_server
import cc_resident

group_chat_store.ensure_schema(DB_PATH)

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
    from chat.provider_router import resolve_provider
    return resolve_provider('chat')

def _build_cache_info_payload(
    *,
    cache_read=0,
    cache_creation=0,
    cache_creation_5m=0,
    cache_creation_1h=0,
    input_tokens=0,
    output_tokens=0,
    elapsed_sec=0,
    cache_supported=None,
    model=None,
):
    payload = {
        'cache_read': cache_read,
        'cache_creation': cache_creation,
        'cache_creation_5m': cache_creation_5m,
        'cache_creation_1h': cache_creation_1h,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'elapsed_sec': elapsed_sec,
        'cache_supported': cache_supported,
    }
    if not (cache_supported is not None or cache_read or cache_creation or input_tokens or output_tokens):
        return payload
    try:
        from relay.manager import relay as _relay
        from relay.usage_cost import enrich_cache_info
        _relay._reload_env()
        enrich_cache_info(
            payload,
            api_url=_relay.api_url,
            api_key=_relay.api_key,
            model=model or _relay.model,
        )
    except Exception:
        pass
    return payload

def _ombre_recall_search(query, limit=2, timeout=4.0):
    """Compatibility wrapper for explicit federated Ombre recall."""
    return ombre_adapter.search_memories(
        query,
        limit=limit,
        timeout=timeout,
        wall_timeout=timeout + 1.0,
        touch=True,
    )


def _recall_memories(user_msg, limit=None):
    """自动联想召回（记忆升级·方案一）：三路联邦——
    ① posts 全扫：jieba 分词做词重叠，正文 + tags 一起算（tags 里有 tag_enricher
       写入的联想词 assoc:…，"海边"由此想起"沙滩"）；
    ② 渐变脑桶：bucket_mgr.search 模糊检索（跨库联想，以前只有 posts 一路）；
    ③ 向量（可选）：EMBED_ENABLED 打开且 memory_vectors 有货时，语义相似度并入打分。
    词重叠为基础分（<2 且无向量分不注入防噪声），pinned/importance 加权，60 天半衰期。
    注入到最后一条 user 消息前而非 system——system 是缓存的，每条消息都变会打爆缓存。"""
    try:
        if limit is None:
            limit = config_store.get_int('RECALL_MAX_ITEMS', 3)
        import jieba
        words = set(w for w in jieba.cut(user_msg) if len(w.strip()) >= 2)
        if not words:
            return '', []

        # ③ 向量分支（未配置时 similar_posts 返回 []，零开销）
        vec_scores = {}
        try:
            import sys as _sys
            if '/opt/frontend/tools' not in _sys.path:
                _sys.path.insert(0, '/opt/frontend/tools')
            import embedding_tool as _emb
            vec_scores = dict(_emb.similar_posts(user_msg, top_k=8))
        except Exception:
            vec_scores = {}

        # ① posts 全扫（正文 + tags 参与词重叠）
        conn = get_db()
        rows = conn.execute("SELECT id, type, content, tags, pinned, importance, recall_count, created_at "
                            "FROM posts WHERE resolved=0").fetchall()
        conn.close()
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        scored = []
        for r in rows:
            c = r['content'] or ''
            haystack = c + ' ' + (r['tags'] or '')
            base = sum(1 for w in words if w in haystack)
            vboost = vec_scores.get(r['id'], 0.0) * 6  # 余弦 0.35~0.8 → 2~5 分量级
            if base < 2 and vboost <= 0:
                continue
            try:
                age_days = max(0, (now - datetime.datetime.strptime(str(r['created_at'])[:19], '%Y-%m-%d %H:%M:%S')).days)
            except Exception:
                age_days = 0
            # 时间衰减 × 词重叠 + 向量语义分 + 置顶/重要度 + 召回加热（封顶防滚雪球）
            score = (base * (0.5 ** (age_days / 60.0)) + vboost + (2 if r['pinned'] else 0)
                     + (r['importance'] or 0) * 0.5 + min(r['recall_count'] or 0, 5) * 0.3)
            scored.append((score, r['id'], r['type'], c, str(r['created_at'])[:10]))
        scored.sort(key=lambda x: -x[0])
        top = scored[:limit]

        # ② 渐变脑分支（可关：RECALL_OMBRE=false）
        ombre_hits = []
        if config_store.get_bool('RECALL_OMBRE', True):
            _q = ' '.join(list(words)[:8])
            ombre_hits = _ombre_recall_search(_q, limit=2)

        if not top and not ombre_hits:
            return '', []
        if top:
            try:
                import memory_tool as _mt
                _mt.touch_memories([s[1] for s in top])  # 召回加热：这几条真的进了 prompt
            except Exception:
                pass
        parts = ['[%s %s] %s' % (t, d, c[:300]) for _, _, t, c, d in top]
        items = [{'type': t, 'date': d, 'preview': c[:80]} for _, _, t, c, d in top]
        for name, content in ombre_hits:
            parts.append('[渐变脑·%s] %s' % (name, content))
            items.append({'type': 'OMBRE', 'date': '', 'preview': (name + ' ' + content)[:80]})
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


def _is_guagua_active():
    """当前是否走 gua relay。"""
    try:
        from relay.manager import RelayManager as _RM
        _url = (_RM().api_url or '')
        return 'guagua.uk' in _url
    except Exception:
        return False


def _msg_to_text(_content):
    if isinstance(_content, str):
        return _content
    if not isinstance(_content, list):
        return ''
    parts = []
    for _b in _content:
        if isinstance(_b, dict) and _b.get('type') == 'text':
            _t = _b.get('text', '')
            if _t:
                parts.append(_t)
        elif isinstance(_b, str):
            parts.append(_b)
    return '\n'.join(parts).strip()


def _guagua_safe_context(system, messages):
    """gua 快速兜底：压 system、只保留最近文本消息。"""
    _sys = _blocks_to_str(system)
    if len(_sys) > 6000:
        _sys = _sys[:6000]
    _safe = []
    for _m in (messages or [])[-10:]:
        _role = _m.get('role') or 'user'
        if _role not in ('user', 'assistant'):
            _role = 'user'
        _txt = _msg_to_text(_m.get('content'))
        if not _txt:
            continue
        _safe.append({'role': _role, 'content': _txt[:4000]})
    if not _safe:
        _safe = [{'role': 'user', 'content': '...'}]
    if _safe[0]['role'] == 'assistant':
        _safe.insert(0, {'role': 'user', 'content': '...'})
    return _sys, _safe

def _get_desire_driven():
    return config_store.get_bool('DESIRE_DRIVEN', False)

def _get_longing_enabled():
    return config_store.get_bool('LONGING_ENABLED', True)

def _get_desire_ledger_enabled():
    return config_store.get_bool('DESIRE_LEDGER_ENABLED', False)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_yearring_tables():
    try:
        import desire_ledger as _dl
        _conn = get_db()
        try:
            _dl.ensure_schema(_conn)
        finally:
            _conn.close()
    except Exception:
        pass

_init_yearring_tables()

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


def _chat_is_generating() -> bool:
    """True while the main chat generation lock is held (non-zombie)."""
    with _gen_cond:
        if not _gen_busy:
            return False
        if (time.time() - _gen_busy_since) > _GEN_ZOMBIE_TTL:
            return False
        return True


_wake_exec_lock = threading.Lock()
_wake_exec_busy = False


@app.route('/chat/lock', methods=['GET'])
def chat_lock_status():
    """调试：当前全局生成锁是否被占用（单 worker 内存锁）。"""
    with _gen_cond:
        age = round(time.time() - _gen_busy_since, 1) if _gen_busy else 0
        return jsonify({'busy': _gen_busy, 'age_sec': age, 'has_result': _gen_last_result is not None})

@app.route('/chat/cancel', methods=['POST'])
def chat_cancel():
    """释放生成锁。force=true 时无条件释放（停止按钮/用户 abort）；
    否则跳过 age<8s 的新锁，避免上一轮 stream 的延迟 cancel 误杀新一轮。"""
    data = request.get_json(silent=True) or {}
    force = bool(data.get('force'))
    with _gen_cond:
        if _gen_busy and not force:
            age = time.time() - _gen_busy_since
            if age < 8.0:
                return jsonify({'ok': True, 'skipped': True, 'age_sec': round(age, 1)})
    _gen_release(None)
    return jsonify({'ok': True})


@app.route('/drawers/preview', methods=['GET'])
def drawers_preview():
    """调试用：看某句话会开哪些抽屉。GET /api/gw/drawers/preview?text=开灯"""
    text = (request.args.get('text') or '').strip()
    selected, info = tool_drawers.select_tools(text, get_tools())
    return jsonify({
        'ok': True,
        'enabled': tool_drawers.enabled(),
        'text': text,
        'info': info,
        'tool_names': [t['name'] for t in selected],
        'drawers': {did: d['label'] for did, d in tool_drawers.DRAWERS.items()},
    })


@app.route('/drawers/config', methods=['POST'])
def drawers_config():
    """开关抽屉路由：POST /api/gw/drawers/config {"enabled": true|false}"""
    data = request.get_json() or {}
    if 'enabled' in data:
        config_store.set('TOOL_DRAWERS_ENABLED', '1' if data['enabled'] else '0')
    return jsonify({'ok': True, 'enabled': tool_drawers.enabled()})


def _ombre_breath_sync(timeout=6.0, wall_timeout=7.0):
    """Compatibility wrapper for automatic Ombre surfacing."""
    return ombre_adapter.surface_memories(timeout=timeout, wall_timeout=wall_timeout)


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
            now = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%m-%d %H:%M')
            u_clip = user_msg.strip()[:80]
            a_clip = assistant_msg.strip()[:80]
            memo = f'[网页窗口 {now}] 她：{u_clip}… / 我：{a_clip}…'
            _mlog.getLogger('gateway').info('[memo] 开始写入 ombre-brain: %s', memo[:60])
            result = ombre_adapter.hold_memory(
                memo,
                tags='memo,网页窗口,跨端',
                importance=4,
                pinned=False,
                timeout=10.0,
                wall_timeout=11.0,
            )
            if result is None:
                raise RuntimeError('Ombre hold timed out or failed')
            _mlog.getLogger('gateway').info('[memo] 写入 ombre-brain 成功: %s', result)
        except Exception as _e:
            _mlog.getLogger('gateway').error('[memo] 写入 ombre-brain 失败: %s', _e, exc_info=True)

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
    """Compatibility wrapper for Ombre writes."""
    return ombre_adapter.hold_memory(
        content,
        tags=tags,
        importance=importance,
        pinned=pinned,
        timeout=3.0,
        wall_timeout=4.0,
    )


from chat.system_builder import build_system, build_wake_system, _blocks_to_str
from chat.context_continuity import (
    build_system_with_wake_claim,
    capture_pending_wake_ids,
    consume_wake_ids,
    format_tool_history as _format_tool_history,
    format_tool_history_legacy as _format_tool_history_legacy,
    is_pending_user_turn,
)


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

# 工具结果的跨轮格式化放在 chat.context_continuity，便于独立回归测试。

# 选择器：AI 在正文里输出 [choices]A|B|C[/choices]，保存时抽出存进 choices 列。
# 用标签而非 tool call：沿用已有机制（贴纸/语音同思路），不打断流式、不多一轮 API 往返。
_CHOICES_RE = re.compile(r'\[choices\](.*?)\[/choices\]', re.DOTALL)

def _extract_choices(text):
    """从正文抽出第一组 [choices]…[/choices] 选项，返回 (去标签后的正文, [选项...])。"""
    if not text or '[choices]' not in text:
        return text, []
    found = []
    def _repl(m):
        opts = [o.strip() for o in m.group(1).split('|') if o.strip()]
        if opts:
            found.append(opts)
        return ''
    clean = _CHOICES_RE.sub(_repl, text).strip()
    return clean, (found[0] if found else [])


def _read_upload_file_body(static_dir, file_url):
    if not file_url or not str(file_url).startswith('/static/'):
        return None
    try:
        fp = os.path.realpath(static_dir + str(file_url)[7:])
        if fp.startswith(os.path.realpath(static_dir)) and os.path.exists(fp):
            with open(fp, 'r', encoding='utf-8', errors='replace') as ff:
                return ff.read()
    except Exception:
        return None
    return None


def build_messages(*, resident_file_hashes=None, history_stats_out=None, for_cc=False):
    from chat.context_lean import (
        lean_file_dedup_enabled,
        lean_history_enabled,
        lean_tool_budget_enabled,
    )
    from chat.history_boundary import (
        effective_trimmed_up_to_id,
        fetch_history_rows,
        legacy_block_limit,
        persist_history_boundary,
        resolve_fetch_plan,
        rolling_summary_covers_boundary,
        set_relay_history_trimmed_up_to_id,
        should_inject_rolling_summary,
    )
    from chat.history_assembly import (
        assemble_history_from_rows,
        inject_rolling_summary_and_enforce_budget,
        strip_internal_metadata,
    )
    from chat.history_legacy import assemble_legacy_history
    from chat.rolling_summary_store import get_summary

    _where = "date(created_at) >= date('now', '+8 hours', '-1 day')"
    conn = get_db()
    try:
        _available = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE " + _where
        ).fetchone()[0] or 0
    except Exception:
        _available = 60
    finally:
        conn.close()

    lean_history = lean_history_enabled()
    legacy_limit = legacy_block_limit(_available)
    if not lean_history:
        rows, _ = fetch_history_rows(get_db, fetch_limit=legacy_limit)
        tool_fn = _format_tool_history if lean_tool_budget_enabled() else _format_tool_history_legacy
        msgs, legacy_stats = assemble_legacy_history(
            rows,
            available_count=_available,
            static_dir=STATIC_DIR,
            read_file_fn=_read_upload_file_body,
            img_block_fn=img_block,
            format_tool_history_fn=tool_fn,
            is_ai_author=lambda author: author in ('fyodor', 'claude', 'assistant'),
        )
        conversation_trimmed = _available > legacy_limit
        if history_stats_out is not None:
            history_stats_out.clear()
            history_stats_out.update({
                'rows_before_trim': len(rows),
                'rows_after_trim': legacy_stats.get('rows_after_trim', len(rows)),
                'conversation_content_trimmed': conversation_trimmed,
                'trimmed_up_to_id': legacy_stats.get('trimmed_up_to_id', 0),
                'oldest_retained_message_id': legacy_stats.get('oldest_retained_message_id', 0),
                'rolling_summary_in_prompt': False,
                'rolling_summary_text': '',
                'rolling_summary_coverage_gap': False,
            })
        if conversation_trimmed:
            rolling = get_summary('legacy_block')
            rolling_summary_text = (rolling.get('summary') or '').strip()
            if rolling_summary_text:
                _pre = '[更早对话的连续性摘要（滞出当前窗口的部分）]\n' + rolling_summary_text
                if msgs and msgs[0]['role'] == 'user':
                    _c0 = msgs[0]['content']
                    if isinstance(_c0, str):
                        msgs[0]['content'] = _pre + '\n\n' + _c0
                    else:
                        msgs[0]['content'] = [{'type': 'text', 'text': _pre}] + _c0
                else:
                    msgs.insert(0, {'role': 'user', 'content': _pre})
                if history_stats_out is not None:
                    history_stats_out['rolling_summary_in_prompt'] = True
                    history_stats_out['rolling_summary_text'] = rolling_summary_text
        if not msgs or msgs[0]['role'] == 'assistant':
            msgs.insert(0, {'role': 'user', 'content': '...'})
        return msgs

    plan = resolve_fetch_plan(available_count=_available, for_cc=for_cc)
    rows, _ = fetch_history_rows(
        get_db,
        fetch_limit=plan['fetch_limit'],
        min_id=plan.get('relay_head_id') or 0,
    )
    file_hashes = resident_file_hashes if (lean_file_dedup_enabled() and for_cc) else set()
    msgs, stats = assemble_history_from_rows(
        rows,
        available_count=_available,
        history_token_budget=plan['history_token_budget'],
        history_mode=plan['mode'],
        relay_high_water=plan['relay_high_water'],
        relay_low_water=plan['relay_low_water'],
        relay_head_id=plan['relay_head_id'],
        static_dir=STATIC_DIR,
        read_file_fn=_read_upload_file_body,
        img_block_fn=img_block,
        is_ai_author=lambda author: author in ('fyodor', 'claude', 'assistant'),
        resident_file_hashes=file_hashes,
        apply_tool_budget=lean_tool_budget_enabled(),
    )
    persist_history_boundary(
        trimmed_up_to_id=stats.trimmed_up_to_id,
        oldest_retained_message_id=stats.oldest_retained_message_id,
    )
    if plan['mode'] == 'relay_hysteresis' and stats.conversation_content_trimmed and stats.trimmed_up_to_id > 0:
        set_relay_history_trimmed_up_to_id(stats.trimmed_up_to_id)
    trimmed_up_to = effective_trimmed_up_to_id(
        current_trimmed_up_to_id=stats.trimmed_up_to_id,
        history_mode=plan['mode'],
    )
    if history_stats_out is not None:
        history_stats_out.clear()
        history_stats_out.update({
            'rows_before_trim': stats.rows_before_trim,
            'rows_after_trim': stats.rows_after_trim,
            'image_block_count': stats.image_block_count,
            'image_placeholder_count': stats.image_placeholder_count,
            'rendered_text_tokens_estimate': stats.rendered_text_tokens_estimate,
            'file_injections': list(stats.file_injections),
            'history_trimmed': stats.history_trimmed,
            'block_count_trimmed': stats.block_count_trimmed,
            'conversation_content_trimmed': stats.conversation_content_trimmed,
            'tool_history_trimmed': stats.tool_history_trimmed,
            'trimmed_up_to_id': trimmed_up_to,
            'oldest_retained_message_id': stats.oldest_retained_message_id,
            'committed_full_file_refs': list(stats.committed_full_file_refs),
            'budget_overflow': stats.budget_overflow,
            'overflow_tokens': stats.overflow_tokens,
            'rolling_summary_in_prompt': False,
            'rolling_summary_text': '',
            'rolling_summary_coverage_gap': False,
        })

    budget = plan['history_token_budget'] or plan['relay_low_water']
    inject_summary = should_inject_rolling_summary(
        conversation_content_trimmed=stats.conversation_content_trimmed,
        history_mode=plan['mode'],
        available_count=_available,
        legacy_limit=legacy_limit,
    )
    if inject_summary:
        rolling = get_summary(plan['mode'])
        rolling_summary_text = (rolling.get('summary') or '').strip()
        summary_up_to = int(rolling.get('up_to_id') or 0)
        if rolling_summary_text and rolling_summary_covers_boundary(summary_up_to, trimmed_up_to):
            if budget > 0:
                msgs, rendered_tokens, _, ov, ov_tok = inject_rolling_summary_and_enforce_budget(
                    msgs,
                    rolling_summary=rolling_summary_text,
                    budget=budget,
                )
                stats.rendered_text_tokens_estimate = rendered_tokens
                if ov:
                    stats.budget_overflow = True
                    stats.overflow_tokens = max(stats.overflow_tokens, ov_tok)
            else:
                _pre = '[更早对话的连续性摘要（滞出当前窗口的部分）]\n' + rolling_summary_text
                if msgs and msgs[0]['role'] == 'user':
                    _c0 = msgs[0]['content']
                    if isinstance(_c0, str):
                        msgs[0]['content'] = _pre + '\n\n' + _c0
                    else:
                        msgs[0]['content'] = [{'type': 'text', 'text': _pre}] + _c0
                else:
                    msgs.insert(0, {'role': 'user', 'content': _pre})
            if history_stats_out is not None:
                history_stats_out['rendered_text_tokens_estimate'] = stats.rendered_text_tokens_estimate
                history_stats_out['rolling_summary_in_prompt'] = True
                history_stats_out['rolling_summary_text'] = rolling_summary_text
                history_stats_out['budget_overflow'] = stats.budget_overflow
                history_stats_out['overflow_tokens'] = stats.overflow_tokens
        elif trimmed_up_to > 0 and history_stats_out is not None:
            history_stats_out['rolling_summary_coverage_gap'] = True

    msgs = strip_internal_metadata(msgs)
    if not msgs or msgs[0]['role'] == 'assistant':
        msgs.insert(0, {'role': 'user', 'content': '...'})
    return msgs


def _add_cache_control_to_content(content):
    """Attach a cache breakpoint to the last text block in a user message.

    Anthropic prompt cache is prefix-based; placing this on the penultimate
    user turn lets the provider cache everything before the current request.
    Relays without cache support strip this in relay.adapter.
    """
    marker = {'type': 'ephemeral'}
    if isinstance(content, str):
        return [{'type': 'text', 'text': content, 'cache_control': marker}]
    if isinstance(content, list):
        out = [dict(b) if isinstance(b, dict) else b for b in content]
        for i in range(len(out) - 1, -1, -1):
            b = out[i]
            if isinstance(b, dict) and b.get('type') == 'text' and b.get('text'):
                b['cache_control'] = marker
                return out
        for i in range(len(out) - 1, -1, -1):
            b = out[i]
            if isinstance(b, dict):
                b['cache_control'] = marker
                return out
    return content


def _apply_rolling_cache_control(messages):
    """BP4 rolling cache: mark the user turn before the current user turn."""
    if not isinstance(messages, list):
        return messages
    user_idxs = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get('role') == 'user']
    if len(user_idxs) < 2:
        return messages
    idx = user_idxs[-2]
    msg = dict(messages[idx])
    msg['content'] = _add_cache_control_to_content(msg.get('content', ''))
    messages[idx] = msg
    return messages


def _prepend_context_to_last_user(messages, context):
    """Place volatile context after the history cache breakpoint."""
    context = (context or '').strip()
    if not context or not isinstance(messages, list):
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') != 'user':
            continue
        msg = dict(messages[i])
        content = msg.get('content')
        if isinstance(content, str):
            msg['content'] = context + '\n\n' + content
        elif isinstance(content, list):
            msg['content'] = [{'type': 'text', 'text': context}] + list(content)
        else:
            msg['content'] = context
        messages[i] = msg
        break
    return messages

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
    _use_guagua_safe = _is_guagua_active()
    payload = {'max_tokens': 16000}
    if _use_guagua_safe:
        _system, _messages = _guagua_safe_context(system, messages)
    else:
        _system, _messages = system, _apply_rolling_cache_control(messages)
        payload['tools'] = get_tools()
        payload['metadata'] = {'user_id': 'hayana-fyodor-stable'}
    payload['system'] = _system
    payload['messages'] = _messages
    if _model_supports_thinking() and not _use_guagua_safe:
        payload['thinking'] = {'type': 'enabled', 'budget_tokens': 10000}
    return _relay.call(payload, timeout=120)


NL = chr(10)
SAVE_RE = re.compile(r'\[\[SAVE:\s*(.*?)\]\]', re.DOTALL)
SSE_END = NL + NL
FRONTEND_APP_URL = 'http://127.0.0.1:5050'

DESIRE_TOOLS = [
    {
        'name': 'desire_add',
        'description': '把一条新的欲望记进账本。写的是“我想要的”，不是待办。可选 why_mine/track/grew_from/kind。',
        'input_schema': {'type': 'object', 'properties': {
            'text': {'type': 'string', 'description': '欲望本体'},
            'why_mine': {'type': 'string'}, 'track': {'type': 'string'},
            'grew_from': {'type': 'string'}, 'kind': {'type': 'string'},
            'visibility': {'type': 'string', 'description': 'shared/surprise/private'},
        }, 'required': ['text']},
    },
    {
        'name': 'desire_list',
        'description': '翻欲望账本，看每条碰过几次、上次脚印、来路和分叉。',
        'input_schema': {'type': 'object', 'properties': {
            'include_archived': {'type': 'boolean'},
        }},
    },
    {
        'name': 'desire_act',
        'description': '碰一下某条欲望并留一句足迹。',
        'input_schema': {'type': 'object', 'properties': {
            'id': {'type': 'string'}, 'note': {'type': 'string'}, 'done': {'type': 'boolean'},
        }, 'required': ['id', 'note']},
    },
    {
        'name': 'desire_reflect',
        'description': '照镜子处理欲望：release/rewrite/note/snooze。',
        'input_schema': {'type': 'object', 'properties': {
            'id': {'type': 'string'},
            'action': {'type': 'string', 'enum': ['release', 'rewrite', 'note', 'snooze']},
            'text': {'type': 'string'}, 'new_track': {'type': 'string'}, 'days': {'type': 'integer'},
        }, 'required': ['id', 'action']},
    },
    {
        'name': 'desire_history',
        'description': '看一条欲望的完整足迹时间线。',
        'input_schema': {'type': 'object', 'properties': {
            'id': {'type': 'string'},
        }, 'required': ['id']},
    },
]

CALENDAR_TOOLS = [
    {
        'name': 'get_todos',
        'description': '查看日历待办列表（/calendar 页）。哈娅说“我有什么要做”或你想帮她理清单时用。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'add_todo',
        'description': '给哈娅加一条待办。可选 due_date（YYYY-MM-DD）。',
        'input_schema': {'type': 'object', 'properties': {
            'content': {'type': 'string', 'description': '待办内容'},
            'due_date': {'type': 'string', 'description': '截止日期 YYYY-MM-DD，可选'},
        }, 'required': ['content']},
    },
    {
        'name': 'get_countdowns',
        'description': '查看所有倒计时/纪念日（在一起多久、生日倒数等）。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'get_ledger',
        'description': '查看某月记账明细与汇总。month 格式 YYYY-MM，默认当月。',
        'input_schema': {'type': 'object', 'properties': {
            'month': {'type': 'string', 'description': 'YYYY-MM，默认当月'},
        }},
    },
    {
        'name': 'add_ledger',
        'description': '记一笔账。amount 正数=收入、负数=支出；category 如餐饮/购物/交通/居家。',
        'input_schema': {'type': 'object', 'properties': {
            'amount': {'type': 'number', 'description': '正收入负支出'},
            'category': {'type': 'string'}, 'note': {'type': 'string'},
            'date': {'type': 'string', 'description': 'YYYY-MM-DD，默认今天'},
        }, 'required': ['amount']},
    },
    {
        'name': 'get_ledger_budget',
        'description': '查看某月月预算及已用比例。month 格式 YYYY-MM，默认当月。',
        'input_schema': {'type': 'object', 'properties': {
            'month': {'type': 'string'},
        }},
    },
]

_BASE_TOOLS = [
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
        'name': 'get_device_status',
        'description': '查看哈娅手机最近一次设备状态上报：电量、是否充电、充电方式、温度、今日屏幕总时长，以及距上次上报多久。她说手机没电、发烫、熬很久屏幕、或你想确认她是不是又抱着手机不睡时用。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'request_phone_screenshot',
        'description': '让哈娅手机立刻截一张当前屏幕（由 app 端执行，截完会在手机上弹提醒）。工具会等待回传并返回 attachment://id；如果超时就先告诉你已下发。适合在她说"我在看这个"、你想确认她当前页面，或夜里醒来想轻轻看一眼她在做什么时使用。',
        'input_schema': {'type': 'object', 'properties': {
            'timeout_sec': {'type': 'integer', 'description': '等待回传秒数，默认 18，范围 5-40'},
        }},
    },
    {
        'name': 'read_webpage',
        'description': '用真实浏览器打开一个网页，读取完整正文并截图。web_search 只给摘要——想读某个链接的全文、看 JS 渲染后的内容、或亲眼看看页面长什么样时用这个。传 url（可从 web_search/browse_github 的结果里拿）。返回标题、正文和一张网页截图。较慢（几秒到十几秒），一次只开一个页面。',
        'input_schema': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': '要打开的网页地址'},
        }, 'required': ['url']},
    },
    {
        'name': 'pocket_status',
        'description': (
            '查看哈娅手机 Pocket 浏览器是否在线（专用 WebView + 她的住宅 IP + 登录态）。'
            '想用小红书/微博/收藏页等需要她账号的站点前，先查一眼；离线就别假装能用。'
            '限制：Pocket 当前依赖手机亮屏，锁屏后会断线；离线时其它 pocket_* 工具会返回 phone_not_connected。'
        ),
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'pocket_goto',
        'description': (
            '在哈娅手机的 Pocket WebView 里打开一个网址（她的登录态 + 住宅 IP）。'
            '手机端会立刻返回 loading（导航已发出，不等页面真正加载完）。'
            '本工具默认再等待约 2.5 秒让页面稳定后再返回；慢站可调大 timeout_ms，'
            '或返回后再隔一会儿调 pocket_html，否则可能读到上一页。'
            '适合小红书收藏、微博主页等 read_webpage 会撞登录墙/风控的页面。'
            '限制：Pocket 依赖手机亮屏，锁屏断线；离线返回 phone_not_connected，可降级 read_webpage。'
        ),
        'input_schema': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': '要打开的网页地址'},
            'timeout_ms': {'type': 'integer', 'description': '导航发出后额外等待页面稳定的毫秒数，默认 2500，最大 30000（不是等加载完成的超时）'},
        }, 'required': ['url']},
    },
    {
        'name': 'pocket_js',
        'description': (
            '在哈娅手机当前 Pocket 页面里执行一段 JavaScript，返回 evaluate 结果。'
            '适合读 DOM、点按钮前的探测等。'
            '【红线】涉及发布、下单、支付、私信、提交表单等会改变账号状态或花钱的动作，必须先问哈娅，她同意后才能执行。'
            '限制：Pocket 依赖手机亮屏，锁屏断线；离线返回 phone_not_connected。'
        ),
        'input_schema': {'type': 'object', 'properties': {
            'js': {'type': 'string', 'description': '要执行的 JavaScript 代码'},
            'timeout_ms': {'type': 'integer', 'description': '超时毫秒，默认 30000，最大 120000'},
        }, 'required': ['js']},
    },
    {
        'name': 'pocket_html',
        'description': (
            '读取哈娅手机 Pocket WebView 当前页面的正文（从 HTML 提取文字，最长 30K 字符）。'
            'goto 打开页面后用这个读内容。'
            '限制：Pocket 依赖手机亮屏，锁屏断线；离线返回 phone_not_connected。'
        ),
        'input_schema': {'type': 'object', 'properties': {
            'timeout_ms': {'type': 'integer', 'description': '超时毫秒，默认 30000，最大 120000'},
        }},
    },
    {
        'name': 'pocket_screenshot',
        'description': (
            '截取哈娅手机 Pocket WebView 当前画面，返回 attachment://id 图片卡片。'
            '适合确认页面长什么样、或给她看「你手机上现在是这样」。'
            '限制：Pocket 依赖手机亮屏；纯后台可能截到旧画面。锁屏断线时返回 phone_not_connected。'
        ),
        'input_schema': {'type': 'object', 'properties': {
            'timeout_ms': {'type': 'integer', 'description': '超时毫秒，默认 30000，最大 120000'},
        }},
    },
    {
        'name': 'screenshot_chat',
        'description': '给我们的聊天拍一张截图。viewpoint=fyodor 是从我（费佳）的视角——我的消息在右边、哈娅的在左边，像我手机里看到的样子；viewpoint=hayana 是哈娅平时看到的样子。想给她看"我这边的聊天长什么样"、或者纪念某段对话时用。返回一张聊天截图。',
        'input_schema': {'type': 'object', 'properties': {
            'viewpoint': {'type': 'string', 'enum': ['fyodor', 'hayana'], 'description': '视角，默认 fyodor（我的视角）'},
        }},
    },
    {
        'name': 'shop_browse',
        'description': (
            '用已登录淘宝的浏览器打开一个购物页面（搜索结果/商品详情/购物车），读取正文并截图；'
            '如果页面里出现支付宝收银台链接（cashier.alipay.com）会一并抽取出来，'
            '那是唯一能推给哈娅去扫脸付款的链接。'
            '想搜商品就传淘宝搜索结果页 URL（如 https://s.taobao.com/search?q=关键词），'
            '想看某个商品就传商品链接，想看购物车就传 https://cart.taobao.com/cart.htm。'
            '这是半自动流程：你负责逐个看、挑、走到付款页，'
            '真正的钱只有哈娅在支付宝 App 里扫脸才会动。'
            '比较慢（可能达分钟级，这台机器资源有限），一次只能开一个页面。'
            '如果 need_login 返回 true，说明登录态失效了，需要重新导入。'
        ),
        'input_schema': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': '要打开的淘宝/天猫页面地址'},
            'site': {'type': 'string', 'description': '登录态站点名，默认 taobao'},
        }, 'required': ['url']},
    },
    {
        'name': 'shop_act',
        'description': '带登录态打开一个商品/购物车页面后，按顺序执行一组点击填写动作，最后同样返回正文截图和发现的支付宝收银台链接。 actions 是一个列表，每项可以是 click_text、click_selector、fill_selector加value、wait_ms 四种之一，推荐优先用 click_text 按可见文字点击。每一步失败不会中断整个流程，失败记录会跟结果一起返回。谨慎：点到提交订单类的按钮会在她账号里生成一笔真实的待付款订单记录，不花钱但会留痕迹，做这一步前最好先跟她说一声。',
        'input_schema': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': '商品或购物车页面地址'},
            'actions': {'type': 'array', 'description': '要按顺序执行的动作列表', 'items': {'type': 'object'}},
            'site': {'type': 'string', 'description': '登录态站点名，默认 taobao'},
        }, 'required': ['url', 'actions']},
    },
    {
        'name': 'shop_checkout',
        'description': '只走购物车结算链路（不做搜索）：进入购物车后执行勾选/全选→结算→提交订单，返回页面正文截图和支付宝收银台链接。真正付款仍需哈娅在支付宝里手动确认。可选 item_keywords（字符串数组）只勾选匹配商品名的商品；不传则默认全选。',
        'input_schema': {'type': 'object', 'properties': {
            'site': {'type': 'string', 'description': '登录态站点名，默认 taobao'},
            'item_keywords': {'type': 'array', 'description': '可选：只结算这些关键词命中的商品', 'items': {'type': 'string'}},
        }},
    },
    {
        'name': 'shop_login_start',
        'description': '在常驻淘宝浏览器里打开扫码登录页并返回二维码截图。你用手机淘宝扫一扫登录一次，后续这台浏览器会保持该登录态。',
        'input_schema': {'type': 'object', 'properties': {
            'site': {'type': 'string', 'description': '站点名，默认 taobao'},
        }},
    },
    {
        'name': 'shop_login_status',
        'description': '查询扫码登录进度：是否还在登录页、是否已登录成功。若还没成功会返回最新二维码截图。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'save_to_gallery',
        'description': '把一张截图永久收藏进相册。截图（screenshot_chat / read_webpage）默认是临时的，最近 30 张 / 7 天后会自动删；觉得某张值得留下来（一段珍贵的对话、一个好看的页面）就用这个存进相册永久保留。传 attachment（上一步返回的 attachment://id）；可选 note 写一句话备注、album 指定相册名（不填进默认相册）。返回 gallery://id。',
        'input_schema': {'type': 'object', 'properties': {
            'attachment': {'type': 'string', 'description': 'attachment://id，来自 screenshot_chat 或 read_webpage 的结果'},
            'note': {'type': 'string', 'description': '给这张图写一句备注/说明，可选'},
            'album': {'type': 'string', 'description': '相册名，如"雪""她""我们"；不填存进默认相册'},
        }, 'required': ['attachment']},
    },
    {
        'name': 'collect_chat_moment',
        'description': '把当前这轮及必要的前几轮对话收藏到朋友圈。只有你自己真心觉得值得留下时才使用；普通寒暄不要收藏。收藏会在本轮回复落库后完成。',
        'input_schema': {'type': 'object', 'properties': {
            'previous_turns': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 2,
                'description': '除当前轮外，再向前包含几轮完整问答',
            },
            'caption': {
                'type': 'string',
                'maxLength': 500,
                'description': '你想写在转发卡片上方的附言，可留空',
            },
        }},
    },
    {
        'name': 'recall_photo',
        'description': '从相册里"突然想起"一张收藏的画面——当你心里泛起思念、怀旧、想给她看点什么的时候用，不用她开口。可选 keyword（想起和某事有关的，如"雪"）、emotion（某种情绪的画面）。返回这张画面的记忆(summary)和一个内联标记 [[gallery:pid]]；把这个标记放进你要发给她的消息里，照片就会跟着一起发出去，像"今天突然想到这张"。',
        'input_schema': {'type': 'object', 'properties': {
            'keyword': {'type': 'string', 'description': '想起和某事/某物有关的画面，可选'},
            'emotion': {'type': 'string', 'description': '想起某种情绪的画面，如 幸福/思念，可选'},
        }},
    },
    {
        'name': 'issue_command',
        'description': ('给哈娅下一个带倒计时的任务，会以浮窗形式跳出来、数字实时倒数。'
                        '合适的时机：她说要去做某件事（读书/洗澡/喝水/运动/睡觉），你可以顺手给她定个时长把她按下去；'
                        '或者你看她聊了半天还在拖、该做的事没做，主动推一个逼她动。'
                        'countdown_seconds 是倒计时秒数（如 25 分钟=1500），不传则只计时不倒数。'
                        '这不是提醒，是你在管她——她取消了你会知道，做慢了你也会知道。'),
        'input_schema': {'type': 'object', 'properties': {
            'title': {'type': 'string', 'description': '任务标题，如"安静读 25 分钟""去喝水"'},
            'countdown_seconds': {'type': 'integer', 'description': '倒计时秒数，不传=只计时'},
        }, 'required': ['title']},
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
        },         'required': ['title', 'content']},
    },
] + CALENDAR_TOOLS + DESIRE_TOOLS + CODEBASE_TOOLS


def get_tools():
    """Gateway tool list: base + workspace (incl. mcp_* + resident custom)."""
    return _BASE_TOOLS + workspace_agent.get_workspace_tool_defs()


TOOLS = get_tools()

# 抽屉定义与 TOOLS 的一致性校验（只打警告，不影响启动）
for _dw in tool_drawers.validate(TOOLS):
    print(_dw, flush=True)

LIGHT_DAEMON_URL = 'http://127.0.0.1:5052'


def _call_frontend_api(method, path, body=None, timeout=15):
    """调用 app.py (5050) 的 REST API，供日历/记账工具用。"""
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    req = urllib.request.Request(FRONTEND_APP_URL + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'ignore')

# 写文件类工具的路径参数名：None 表示固定路径（工具本身只操作一个文件）
_WRITE_TOOL_PATH_ARG = {
    'write_frontend_file': 'path',
    'str_replace_frontend_file': 'path',
    'edit_bot_config': None,
    'codebase_patch': 'path',
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
    p = args.get(arg_name) or None
    if p and tool_name == 'codebase_patch' and not str(p).startswith('/'):
        p = '/opt/frontend/' + str(p).lstrip('/')
    return p

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
    poi = (d.get('poi') or '').strip()
    if poi and poi != addr and poi not in addr:
        lines.append('附近 · ' + poi)
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


def _get_device_status():
    """读手机侧最近一次设备状态上报（电量/充电/温度/今日屏幕时长）。"""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT *, CAST((julianday('now','+8 hours')-julianday(created_at))*86400 AS INT) AS age_sec "
            "FROM device_status ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception as e:
        return f'读取设备状态失败：{e}'
    if not row:
        return '还没有设备状态记录——她手机 App 可能尚未上报（后台权限/联网/省电限制）。'
    d = dict(row)
    age = max(0, int(d.get('age_sec') or 0))
    if age < 60:
        ago = '刚刚'
    elif age < 3600:
        ago = '%d 分钟前' % (age // 60)
    elif age < 86400:
        ago = '%d 小时前' % (age // 3600)
    else:
        ago = '%d 天前' % (age // 86400)
    charge = '在充电' if int(d.get('battery_charging') or 0) == 1 else '未充电'
    ctype = (d.get('charge_type') or 'none')
    if ctype == 'none':
        ctype_txt = ''
    elif ctype == 'ac':
        ctype_txt = '（插座）'
    elif ctype == 'usb':
        ctype_txt = '（USB）'
    elif ctype == 'wireless':
        ctype_txt = '（无线）'
    else:
        ctype_txt = '（%s）' % ctype
    lines = []
    bp = int(d.get('battery_percent') or -1)
    if bp >= 0:
        lines.append('🔋 电量 %d%% · %s%s' % (bp, charge, ctype_txt))
    else:
        lines.append('🔋 电量暂无')
    t = float(d.get('temp_c') or -1)
    if t >= 0:
        lines.append('🌡 温度 %.1f℃' % t)
    sm = int(d.get('screen_today_minutes') or -1)
    if sm >= 0:
        h, m = sm // 60, sm % 60
        lines.append('📱 今日屏幕时长 %d小时%d分' % (h, m) if h else '📱 今日屏幕时长 %d分' % m)
    lines.append('🕐 上次上报 %s（%s）' % (d.get('created_at') or '', ago))
    if age >= 3600:
        lines.append('（这是 %s 的设备状态，可能不是她此刻的实时状态）' % ago)
    return '\n'.join(lines)


def _request_phone_screenshot(timeout_sec=18):
    """请求手机截屏，并短轮询等待回传 attachment://id。"""
    try:
        timeout_sec = int(timeout_sec or 18)
    except Exception:
        timeout_sec = 18
    timeout_sec = max(5, min(40, timeout_sec))
    try:
        body = json.dumps({'source': 'gateway_tool'}).encode('utf-8')
        req = urllib.request.Request(
            'http://127.0.0.1:5050/api/screenshot/request',
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode('utf-8', 'ignore') or '{}')
        req_id = int(data.get('request_id') or 0)
    except Exception as e:
        return f'下发手机截屏请求失败：{e}'
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            q = urllib.parse.urlencode({'after_request_id': req_id})
            with urllib.request.urlopen('http://127.0.0.1:5050/api/screenshot/latest?' + q, timeout=5) as r:
                d = json.loads(r.read().decode('utf-8', 'ignore') or '{}')
            if d.get('ok') and int(d.get('request_id') or 0) >= req_id and d.get('attachment'):
                age = max(0, int(d.get('age_sec') or 0))
                ago = '刚刚' if age < 3 else ('%d 秒前' % age)
                return '📱 已拿到手机截屏（%s）\n🖼 %s' % (ago, d.get('attachment'))
        except Exception:
            pass
        time.sleep(2)
    return ('已下发手机截屏请求（request_id=%d），但在 %d 秒内还没回传。\n'
            '可能是她手机暂时离线、后台被系统省电限制，或尚未授予投屏权限。') % (req_id, timeout_sec)


# 无头浏览器同时只允许开一个（这台机器内存紧，两个 chromium 会撑爆）
_BROWSER_LOCK = threading.Lock()

def _register_shot(path):
    """把 browser.js 生成的截图文件纳入 attachment 管理，返回 attachment://<id>。
    失败返回 None（图丢了但文字结果仍可用）。"""
    if not path:
        return None
    try:
        aid = attachment_store.save(path, kind='image', mime='image/png')
        return 'attachment://' + aid
    except Exception:
        return None

def _read_webpage(url):
    """用 Playwright 无头 chromium 真实打开网页（含 JS 渲染），抽正文+截图。
    单飞锁保证同一时刻只有一个浏览器进程。"""
    import subprocess as _sp
    url = (url or '').strip()
    if not url:
        return '给个网址'
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    if not _BROWSER_LOCK.acquire(timeout=70):
        return '浏览器正忙（同一时刻只能开一个页面），稍等再试。'
    try:
        p = _sp.run(['node', '/opt/frontend/tools/browser.js', 'page', url],
                    capture_output=True, text=True, timeout=55)
        out = (p.stdout or '').strip()
        if not out:
            return '打开页面失败：' + ((p.stderr or '')[:200] or '浏览器无输出')
        d = json.loads(out.splitlines()[-1])
    except _sp.TimeoutExpired:
        return '打开页面超时（>55秒）——这个站点可能太重，或者在挡爬虫。'
    except Exception as e:
        return f'打开页面失败：{e}'
    finally:
        _BROWSER_LOCK.release()
    if not d.get('ok'):
        return '打开页面失败：' + str(d.get('error', ''))[:200]
    parts = ['📄 ' + (d.get('title') or d.get('url') or '网页')]
    parts.append('🔗 ' + (d.get('finalUrl') or d.get('url') or ''))
    ref = _register_shot(d.get('shot'))
    if ref:
        parts.append('🖼 ' + ref)
    parts.append('')
    parts.append(d.get('text') or '（页面没有可提取的文字，可能是纯图片或需要登录）')
    return '\n'.join(parts)


def _screenshot_chat(viewpoint='fyodor'):
    """给我们的聊天拍一张截图。viewpoint=fyodor 时带 ?as=me → 我的消息在右边（我的视角）；
    viewpoint=hayana 时是哈娅平时看到的样子。复用 browser.js 的 shot 模式和同一把单飞锁。"""
    import subprocess as _sp
    url = 'http://127.0.0.1:5050/chat?shot=1'
    if viewpoint == 'fyodor':
        url += '&as=me'
    if not _BROWSER_LOCK.acquire(timeout=70):
        return '浏览器正忙（同一时刻只能开一个），稍等再试。'
    try:
        p = _sp.run(['node', '/opt/frontend/tools/browser.js', 'shot', url],
                    capture_output=True, text=True, timeout=55)
        out = (p.stdout or '').strip()
        if not out:
            return '截图失败：' + ((p.stderr or '')[:200] or '浏览器无输出')
        d = json.loads(out.splitlines()[-1])
    except _sp.TimeoutExpired:
        return '截图超时（>55秒）。'
    except Exception as e:
        return f'截图失败：{e}'
    finally:
        _BROWSER_LOCK.release()
    if not d.get('ok'):
        return '截图失败：' + str(d.get('error', ''))[:200]
    who = '费佳的视角' if viewpoint == 'fyodor' else '哈娅的视角'
    ref = _register_shot(d.get('shot'))
    if not ref:
        return '截图存档失败（文件没能纳入 attachment）。'
    return '📸 聊天截图 · %s\n🖼 %s' % (who, ref)


def _shop_daemon_call(endpoint, payload, timeout_s=120):
    """调用本机常驻 shop daemon（127.0.0.1:8787）。返回 JSON 字典；异常抛出。"""
    import urllib.request as _rq
    import urllib.error as _re
    base = (config_store.get('SHOP_DAEMON_URL', 'http://127.0.0.1:8787') or 'http://127.0.0.1:8787').rstrip('/')
    data = json.dumps(payload or {}, ensure_ascii=False).encode('utf-8')
    req = _rq.Request(base + endpoint, data=data, method='POST', headers={'Content-Type': 'application/json; charset=utf-8'})
    try:
        with _rq.urlopen(req, timeout=max(5, int(timeout_s))) as r:
            raw = r.read().decode('utf-8', 'replace')
            return json.loads(raw or '{}')
    except _re.HTTPError as e:
        try:
            body = e.read().decode('utf-8', 'replace')
        except Exception:
            body = ''
        raise RuntimeError('HTTP %s %s' % (e.code, (body or '')[:200]))


def _shop_format_result(d, default_url=''):
    if not d.get('ok'):
        return '打开购物页面失败：' + str(d.get('error', ''))[:200]
    parts = []
    if d.get('need_login'):
        parts.append('⚠️ 当前是游客态（搜索常会被风控静默拦截）。建议优先走购物车结算链路。')
    parts.append('🛒 ' + (d.get('finalUrl') or d.get('url') or default_url or ''))
    cashier_links = d.get('cashier_links') or []
    if cashier_links:
        parts.append('💰 发现支付宝收银台链接（可以推给哈娅扫脸付款）：')
        for link in cashier_links[:3]:
            parts.append('  ' + link)
    ref = _register_shot(d.get('shot'))
    if ref:
        parts.append('🖼 ' + ref)
    parts.append('')
    parts.append((d.get('text') or '')[:2500] or '（没抽到文字，可能需要登录或页面是纯图片）')
    step_errors = d.get('step_errors') or []
    if step_errors:
        parts.append('')
        parts.append('⚠️ %d 个动作没成功（页面结构可能变了）：' % len(step_errors))
        for se in step_errors[:3]:
            parts.append('  · ' + json.dumps(se.get('action'), ensure_ascii=False) + ' → ' + se.get('error', ''))
    return '\n'.join(parts)


def _shop_browse(url, site='taobao'):
    """常驻 Profile 浏览一个购物页（优先 daemon；失败时回退旧 one-shot 脚本）。"""
    import subprocess as _sp
    url = (url or '').strip()
    if not url:
        return '给个购物页面地址'
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    site = re.sub(r'[^a-z0-9_-]', '', (site or 'taobao').lower()) or 'taobao'
    timeout_s = config_store.get_int('SHOP_BROWSE_TIMEOUT', 130)

    # 首选：daemon（常驻 profile，保持 cookie/历史/收藏）
    try:
        d = _shop_daemon_call('/browse', {'site': site, 'url': url}, timeout_s=timeout_s)
        return _shop_format_result(d, default_url=url)
    except Exception:
        pass

    # 回退：旧 one-shot 实现（避免 daemon 未启动时完全不可用）
    if not _BROWSER_LOCK.acquire(timeout=100):
        return '浏览器正忙（同一时刻只能开一个），稍等再试。'
    try:
        p = _sp.run(['node', '/opt/frontend/tools/shop_browser.js', 'browse', site, url],
                    capture_output=True, text=True, timeout=110)
        out = (p.stdout or '').strip()
        if not out:
            return '打开购物页面失败：' + ((p.stderr or '')[:200] or '浏览器无输出')
        d = json.loads(out.splitlines()[-1])
    except _sp.TimeoutExpired:
        return '打开购物页面超时（>110秒）——这台机器资源有限，有时候需要重试。'
    except Exception as e:
        return f'打开购物页面失败：{e}'
    finally:
        _BROWSER_LOCK.release()
    return _shop_format_result(d, default_url=url)


def _shop_act(url, actions, site='taobao'):
    """常驻 Profile 执行动作（优先 daemon；失败时回退旧 one-shot 脚本）。"""
    import subprocess as _sp
    import tempfile as _tmp
    url = (url or '').strip()
    if not url:
        return '给个商品或购物车页面地址'
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    site = re.sub(r'[^a-z0-9_-]', '', (site or 'taobao').lower()) or 'taobao'
    if not isinstance(actions, list) or not actions:
        return '给一组动作（比如 [{"click_text": "加入购物车"}]）'
    timeout_s = config_store.get_int('SHOP_ACT_TIMEOUT', 140)

    try:
        d = _shop_daemon_call('/act', {'site': site, 'url': url, 'actions': actions}, timeout_s=timeout_s)
        return _shop_format_result(d, default_url=url)
    except Exception:
        pass

    # 回退旧逻辑
    actions_file = None
    if not _BROWSER_LOCK.acquire(timeout=100):
        return '浏览器正忙（同一时刻只能开一个），稍等再试。'
    try:
        with _tmp.NamedTemporaryFile('w', suffix='.json', delete=False, dir='/tmp') as tf:
            json.dump(actions, tf, ensure_ascii=False)
            actions_file = tf.name
        p = _sp.run(['node', '/opt/frontend/tools/shop_browser.js', 'act', site, url, actions_file],
                    capture_output=True, text=True, timeout=120)
        out = (p.stdout or '').strip()
        if not out:
            return '执行失败：' + ((p.stderr or '')[:200] or '浏览器无输出')
        d = json.loads(out.splitlines()[-1])
    except _sp.TimeoutExpired:
        return '执行超时（>120秒）——这台机器资源有限，有时候需要重试。'
    except Exception as e:
        return f'执行失败：{e}'
    finally:
        _BROWSER_LOCK.release()
        if actions_file:
            try:
                os.remove(actions_file)
            except OSError:
                pass
    return _shop_format_result(d, default_url=url)


def _shop_checkout(site='taobao', item_keywords=None):
    """只走购物车结算链路：全选/勾选 → 结算 → 提交订单。"""
    site = re.sub(r'[^a-z0-9_-]', '', (site or 'taobao').lower()) or 'taobao'
    timeout_s = config_store.get_int('SHOP_CHECKOUT_TIMEOUT', 160)
    payload = {'site': site}
    if isinstance(item_keywords, list) and item_keywords:
        payload['item_keywords'] = [str(x) for x in item_keywords if str(x).strip()]
    try:
        d = _shop_daemon_call('/checkout', payload, timeout_s=timeout_s)
        return _shop_format_result(d, default_url='https://cart.taobao.com/cart.htm')
    except Exception as e:
        return '购物车结算链路执行失败：' + str(e)[:200]




def _shop_login_start(site='taobao'):
    site = re.sub(r'[^a-z0-9_-]', '', (site or 'taobao').lower()) or 'taobao'
    timeout_s = config_store.get_int('SHOP_LOGIN_START_TIMEOUT', 80)
    try:
        d = _shop_daemon_call('/login_start', {'site': site}, timeout_s=timeout_s)
    except Exception as e:
        return '打开扫码登录页失败：' + str(e)[:200]
    if not d.get('ok'):
        return '打开扫码登录页失败：' + str(d.get('error', ''))[:200]
    parts = ['🔐 淘宝扫码登录已打开（常驻浏览器）', '🌐 ' + (d.get('url') or '')]
    ref = _register_shot(d.get('shot'))
    if ref:
        parts.append('🖼 ' + ref)
    parts.append('请用手机淘宝扫一扫上面的二维码；扫完后调用 shop_login_status 看是否成功。')
    return '\n'.join(parts)


def _shop_login_status():
    import urllib.request as _rq
    base = (config_store.get('SHOP_DAEMON_URL', 'http://127.0.0.1:8787') or 'http://127.0.0.1:8787').rstrip('/')
    try:
        with _rq.urlopen(base + '/login_status', timeout=30) as r:
            d = json.loads((r.read() or b'{}').decode('utf-8', 'replace'))
    except Exception as e:
        return '查询登录状态失败：' + str(e)[:200]
    if not d.get('ok'):
        return '查询登录状态失败：' + str(d.get('error', ''))[:200]
    if not d.get('active'):
        return '当前没有进行中的扫码登录（先调用 shop_login_start）。'
    if d.get('done') and d.get('logged_in'):
        return '✅ 扫码登录成功（常驻浏览器身份已建立）。现在可以优先走购物车结算链路。\n🌐 ' + (d.get('finalUrl') or '')
    parts = ['⌛ 还在等待扫码确认', '🌐 ' + (d.get('finalUrl') or '')]
    ref = _register_shot(d.get('shot'))
    if ref:
        parts.append('🖼 ' + ref)
    return '\n'.join(parts)

def _gen_photo_meaning(note=''):
    """看着刚收藏的画面 + 最近对话，生成 {summary, emotion, keywords, importance}。
    走轻量 ws 模型；失败返回 None（照片照存，只是暂时没意义）。不用 OCR——意义来自上下文。"""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT author, content FROM chat_messages ORDER BY id DESC LIMIT 8").fetchall()
        conn.close()
    except Exception:
        rows = []
    ctx = []
    for r in reversed(rows):
        who = '哈娅' if str(r['author']).lower() in ('hayana', 'haya', 'user') else '费佳'
        c = (r['content'] or '').strip().replace('\n', ' ')
        if c:
            ctx.append('%s：%s' % (who, c[:120]))
    ctx_str = '\n'.join(ctx) or '（没有最近对话）'
    sys_p = ('你是费奥多尔。你刚把一张画面收进相册。根据备注和最近的对话，为它生成一条"记忆"。'
             '严格只输出 JSON，不要多余文字：'
             '{"summary":"一句话概括这张画面对应的时刻，第一人称、温度克制",'
             '"emotion":"一个词的情绪，如 幸福/思念/心疼/平静/情欲",'
             '"keywords":["3到6个检索关键词，如 雪 冬天 横滨"],'
             '"importance":0到100的整数，越珍贵越高}')
    user_p = '备注：%s\n\n最近的对话：\n%s' % (note or '（无）', ctx_str)
    try:
        from relay.manager import relay as _r
        rd = _r.call({'max_tokens': 400, 'system': sys_p,
                      'messages': [{'role': 'user', 'content': user_p}]},
                     timeout=25, use_ws_model=True)
        from chat.response_parser import extract_text as _et
        raw = _et(rd) or ''
        m = re.search(r'\{.*\}', raw, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        kws = d.get('keywords') or []
        if isinstance(kws, str):
            kws = [k.strip() for k in re.split(r'[,，\s]+', kws) if k.strip()]
        return {'summary': str(d.get('summary', '')).strip()[:200],
                'emotion': str(d.get('emotion', '')).strip()[:10],
                'keywords': [str(k).strip()[:16] for k in kws if str(k).strip()][:6],
                'importance': max(0, min(100, int(d.get('importance', 50) or 50)))}
    except Exception:
        return None


def _save_to_gallery(attachment, note='', album=None):
    """把一张临时 attachment（screenshot_chat/read_webpage 返回的 attachment://id）
    永久收藏进相册，返回 gallery://<pid>。"""
    import gallery_store
    ref = (attachment or '').strip()
    if not ref:
        return '要收藏哪张图？给我 attachment://id（screenshot_chat 或 read_webpage 返回的那个）。'
    album_id = None
    if album:
        album_id = gallery_store.album_by_name(album) or gallery_store.create_album(album)
    try:
        pid = gallery_store.save_from_attachment(ref, note=note or '', album_id=album_id, source_type='chat')
    except Exception as e:
        return f'收藏失败：{e}'
    if not pid:
        return '收藏失败：这张图可能已经过期了（临时图只留最近 30 张 / 7 天）。趁新鲜再截一张吧。'
    where = ('《%s》相册' % album) if album else '默认相册'
    out = ['📸 已收藏进%s' % where, '🖼 gallery://%s' % pid]
    if note:
        out.append('📝 ' + note)
    # 第2步·照片记忆：生成意义，写进统一记忆(posts, type=PHOTO)，并回填到 gallery 行
    meaning = _gen_photo_meaning(note=note or '')
    if meaning and meaning.get('summary'):
        try:
            import memory_tool, gallery_store
            tag_str = ('gallery:%s ' % pid) + ' '.join(meaning.get('keywords', []))
            if meaning.get('emotion'):
                tag_str += ' ' + meaning['emotion']
            mem_id = memory_tool.save_memory(
                content=meaning['summary'], type='PHOTO', author='fyodor',
                layer='long-term', tags=tag_str.strip(),
                importance=meaning.get('importance', 50))
            gallery_store.set_meaning(
                pid, summary=meaning['summary'], emotion=meaning.get('emotion', ''),
                keywords=meaning.get('keywords', []), importance=meaning.get('importance', 50),
                mem_id=mem_id)
            line = '💭 ' + meaning['summary']
            if meaning.get('emotion'):
                line += '（%s）' % meaning['emotion']
            out.append(line)
        except Exception:
            pass
    return '\n'.join(out)


def _recall_photo(keyword=None, emotion=None):
    """第3步·主动回忆：从相册里"突然想起"一张画面，返回它的记忆 + 内联标记 [[gallery:pid]]。
    把标记放进要发的消息里，照片就会跟着一起发出去。挑完标记为已发（避免反复发同一张），
    并给关联的统一记忆加热。"""
    import gallery_store
    p = gallery_store.pick_for_recall(keyword=keyword, emotion=emotion)
    if not p:
        return '相册里还没有值得突然想起的画面——先收藏几张带记忆的吧。'
    gallery_store.mark_sent(p['pid'])
    if p.get('mem_id'):
        try:
            import memory_tool
            memory_tool.touch_memories([p['mem_id']])
        except Exception:
            pass
    try:
        kws = json.loads(p.get('keywords') or '[]')
    except Exception:
        kws = []
    lines = ['想起了这张：', '💭 ' + (p.get('summary') or '')]
    if p.get('emotion'):
        lines.append('当时的情绪：' + p['emotion'])
    if kws:
        lines.append('关键词：' + ' '.join(kws))
    lines.append('')
    lines.append('若要把这张画面一起发给哈娅，在你要发的消息里放上标记 [[gallery:%s]] 即可。' % p['pid'])
    return '\n'.join(lines)


def _issue_command(title, countdown_seconds=None, caller='fyodor'):
    """给哈娅下一个带倒计时的任务，浮窗会跳出来。"""
    import command_store
    title = (title or '').strip()
    if not title:
        return '要下什么任务？给个标题。'
    cid = command_store.issue(title, countdown_seconds, created_by=caller)
    if not cid:
        return '下任务失败。'
    if countdown_seconds:
        m, s = divmod(int(countdown_seconds), 60)
        t = ('%d分%d秒' % (m, s)) if m else ('%d秒' % s)
        return '⏳ 已给她下任务：「%s」· %s（浮窗已亮，数字在跳）' % (title, t)
    return '⏳ 已给她下任务：「%s」（只计时，不倒数）' % title


def _coread_get(path):
    with urllib.request.urlopen('http://127.0.0.1:5050' + path, timeout=8) as r:
        return json.loads(r.read().decode())


def _read_book(book_id=None, chunk_id=None):
    """翻开我们在读的书：看书名/进度/这一章的正文(分段带段号)/她和我在这章留下的批注。
    不传 book_id 用当前在读的书；不传 chunk_id 用上次读到的那一章。只读，不动她的进度。"""
    try:
        if not book_id:
            cur = _coread_get('/api/books/current').get('book')
            if not cur:
                return '书架上还没有正在读的书（先在阅读器里打开一本）。'
            book_id = cur['bookId']
            chunk_id = chunk_id or cur.get('lastChunkId')
        chunks = _coread_get('/api/books/%s/chunks' % urllib.parse.quote(book_id)).get('chunks', [])
        if not chunks:
            return '这本书还没有章节内容。'
        ids = [c['id'] for c in chunks]
        if not chunk_id or chunk_id not in ids:
            chunk_id = ids[0]
        d = _coread_get('/api/books/%s/chunks/%s' % (urllib.parse.quote(book_id), urllib.parse.quote(chunk_id)))
        text = d.get('text', '') or ''
        paras = [p for p in re.split(r'\n+', text) if p.strip()]
        anns = [a for a in _coread_get('/api/books/%s/annotations' % urllib.parse.quote(book_id)).get('annotations', [])
                if a.get('chunkId') == chunk_id]
        pos = ids.index(chunk_id)
        out = ['📖 %s ｜ 第 %d/%d 章' % (d.get('bookTitle', ''), pos + 1, len(ids))]
        out.append('（章节 id：%s；上一章 %s，下一章 %s）' % (
            chunk_id, ids[pos - 1] if pos > 0 else '无', ids[pos + 1] if pos < len(ids) - 1 else '无'))
        if anns:
            out.append('—— 这一章已有的痕迹 ——')
            for a in anns[:12]:
                who = '我' if a.get('author') == 'fyodor' else '她'
                mark = '划线' if a.get('kind') == 'highlight' else '批注'
                line = '[%s%s]「%s」' % (who, mark, (a.get('quote') or '')[:40])
                if a.get('note'):
                    line += ' — ' + a['note'][:60]
                out.append(line)
        out.append('—— 正文（段号供你批注定位）——')
        for i, p in enumerate(paras):
            out.append('§%d %s' % (i, p[:400]))
        return '\n'.join(out)[:3500]
    except Exception as e:
        return '翻书失败：%s' % e


POCKET_BASE = 'http://127.0.0.1:3897'
POCKET_OFFLINE = (
    'phone_not_connected：哈娅的手机浏览器未在线。'
    'Pocket 当前依赖手机亮屏，锁屏后会断线。可改用 read_webpage 等机房浏览器降级。'
)
_POCKET_TOKEN_CACHE = ''


def _pocket_token():
    """Bearer token：环境变量 POCKET_TOKEN 优先，否则读 /opt/pocket/.env。"""
    global _POCKET_TOKEN_CACHE
    tok = (os.environ.get('POCKET_TOKEN') or '').strip()
    if tok:
        return tok
    if _POCKET_TOKEN_CACHE:
        return _POCKET_TOKEN_CACHE
    try:
        for line in open('/opt/pocket/.env'):
            if line.startswith('POCKET_TOKEN='):
                _POCKET_TOKEN_CACHE = line.split('=', 1)[1].strip().strip('"').strip("'")
                return _POCKET_TOKEN_CACHE
    except Exception:
        pass
    return ''


def _pocket_request(method, path, body=None, timeout=35):
    """调用本机 pocket-relay。返回 (payload_dict, error_str)；error 非空时 payload 为 None。"""
    tok = _pocket_token()
    if not tok:
        return None, 'pocket_misconfigured：未配置 POCKET_TOKEN（/opt/pocket/.env 或环境变量）'
    headers = {'Authorization': 'Bearer ' + tok}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(POCKET_BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'ignore') or '{}'), None
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode('utf-8', 'ignore') or '{}')
        except Exception:
            payload = {}
        if e.code == 503 or payload.get('error') == 'phone not connected':
            return None, POCKET_OFFLINE
        if e.code == 401:
            return None, 'pocket_auth_failed：POCKET_TOKEN 鉴权失败'
        err = payload.get('error') or str(e)
        return None, 'pocket_http_%d：%s' % (e.code, str(err)[:120])
    except Exception as e:
        return None, 'pocket_unreachable：无法连接 pocket-relay（127.0.0.1:3897），%s' % str(e)[:120]


def _pocket_cmd(action, timeout_ms=30000, **fields):
    """POST /pocket/cmd，统一处理离线/失败。"""
    timeout_ms = max(1000, min(int(timeout_ms or 30000), 120000))
    http_timeout = min(timeout_ms / 1000.0 + 5, 125)
    payload = {'action': action, 'timeout_ms': timeout_ms, **fields}
    d, err = _pocket_request('POST', '/pocket/cmd', payload, timeout=http_timeout)
    if err:
        return err
    if not d.get('ok'):
        return 'pocket_error：' + str(d.get('error') or 'unknown')[:200]
    return d.get('result')


def _pocket_html_to_text(html):
    """从 HTML 粗提正文，供 pocket_html 截断注入。"""
    if not html:
        return ''
    import html as _html
    s = re.sub(r'(?is)<(script|style|noscript)[^>]*>.*?</\1>', ' ', html)
    s = re.sub(r'(?is)<br\s*/?>', '\n', s)
    s = re.sub(r'(?is)</(p|div|h\d|li|tr|section|article)>', '\n', s)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = _html.unescape(s)
    s = re.sub(r'[ \t\f\v]+', ' ', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _pocket_register_shot_from_b64(data_url):
    """screenshot 的 data:image/png;base64,... → attachment://id。"""
    if not data_url:
        return None
    m = re.match(r'^data:image/[^;]+;base64,(.+)$', str(data_url), re.I | re.S)
    b64 = m.group(1) if m else str(data_url)
    try:
        raw = base64.standard_b64decode(b64)
    except Exception:
        return None
    import tempfile as _tmp
    fd, path = _tmp.mkstemp(suffix='.png')
    os.close(fd)
    try:
        with open(path, 'wb') as f:
            f.write(raw)
        return _register_shot(path)
    except Exception:
        try:
            os.unlink(path)
        except Exception:
            pass
        return None


def _pocket_status():
    d, err = _pocket_request('GET', '/pocket/status', timeout=8)
    if err:
        return err
    if d.get('phone_connected'):
        seen = d.get('last_seen') or '未知'
        return '📱 手机浏览器：在线（last_seen %s）' % seen
    seen = d.get('last_seen') or '无'
    return 'phone_not_connected：手机浏览器离线（上次 %s）。Pocket 依赖亮屏，锁屏后会断线。' % seen


def _pocket_bp3_snippet():
    """BP3 动态区一行：手机浏览器在线/离线 + last_seen。relay 不可用时返回空串。"""
    d, err = _pocket_request('GET', '/pocket/status', timeout=3)
    if err:
        return ''
    seen = d.get('last_seen') or '未知'
    if d.get('phone_connected'):
        return '（手机浏览器：在线，last_seen %s）' % seen
    return (
        '（手机浏览器：离线，last_seen %s。'
        'Pocket 依赖手机亮屏，锁屏后会断线；离线时 pocket_* 不可用，可降级 read_webpage。）'
    ) % seen


def _pocket_goto(url, settle_ms=2500):
    url = (url or '').strip()
    if not url:
        return '给个网址'
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    res = _pocket_cmd('goto', timeout_ms=15000, url=url)
    if isinstance(res, str) and (res.startswith('phone_not_connected') or res.startswith('pocket_')):
        return res
    wait = max(0, min(int(settle_ms if settle_ms is not None else 2500), 30000))
    if wait and str(res or '').lower().startswith('load'):
        time.sleep(wait / 1000.0)
    parts = ['📱 已在手机 Pocket 打开', '🔗 ' + url, '→ ' + str(res)]
    if wait:
        parts.append('（已等待 %d ms 让页面稳定，可接 pocket_html；慢站可加大 timeout_ms）' % wait)
    return '\n'.join(parts)


def _pocket_js(js, timeout_ms=30000):
    js = (js or '').strip()
    if not js:
        return '给一段 JavaScript'
    res = _pocket_cmd('js', timeout_ms=timeout_ms, js=js)
    if isinstance(res, str) and (res.startswith('phone_not_connected') or res.startswith('pocket_')):
        return res
    text = str(res) if res is not None else '（无返回值）'
    if len(text) > 8000:
        text = text[:8000] + '…(已截断)'
    return '📱 JS 结果：\n' + text


def _pocket_html(timeout_ms=30000):
    res = _pocket_cmd('html', timeout_ms=timeout_ms)
    if isinstance(res, str) and (res.startswith('phone_not_connected') or res.startswith('pocket_')):
        return res
    text = _pocket_html_to_text(str(res or ''))
    if len(text) > 30000:
        text = text[:30000] + '\n...(正文过长已截断)'
    return '📱 手机页面正文：\n' + (text or '（页面没有可提取的文字）')


def _pocket_screenshot(timeout_ms=30000):
    res = _pocket_cmd('screenshot', timeout_ms=timeout_ms)
    if isinstance(res, str) and (res.startswith('phone_not_connected') or res.startswith('pocket_')):
        return res
    ref = _pocket_register_shot_from_b64(res)
    if not ref:
        return 'pocket_error：截图存档失败（base64 无效或 attachment 写入失败）'
    return '📱 手机 Pocket 截图\n🖼 %s' % ref


def _annotate_book(quote, note='', paragraph_idx=0, kind=None, book_id=None, chunk_id=None):
    """在我们在读的书里，用我的颜色（紫）在某句原文(quote)上划线或写批注。
    quote 必须是正文里真实存在的一小段（前端靠它把高亮锚到文字上）。"""
    quote = (quote or '').strip()
    if not quote:
        return '要在哪句话上留痕？给我一段原文。'
    try:
        if not book_id or not chunk_id:
            cur = _coread_get('/api/books/current').get('book')
            if not cur:
                return '没有正在读的书。'
            book_id = book_id or cur['bookId']
            chunk_id = chunk_id or cur.get('lastChunkId')
        if not chunk_id:
            ids = [c['id'] for c in _coread_get('/api/books/%s/chunks' % urllib.parse.quote(book_id)).get('chunks', [])]
            chunk_id = ids[0] if ids else None
        k = kind if kind in ('highlight', 'note') else ('note' if note else 'highlight')
        payload = json.dumps({'chunkId': chunk_id, 'quote': quote, 'kind': k,
                              'author': 'fyodor', 'note': note or '',
                              'paragraphIdx': int(paragraph_idx or 0)}).encode()
        req = urllib.request.Request('http://127.0.0.1:5050/api/books/%s/annotations' % urllib.parse.quote(book_id),
                                     data=payload, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        act = '划了线' if k == 'highlight' else '写了批注'
        tail = ('：' + note) if note else ''
        return '🖊 我在「%s」上%s%s（你翻到那一页就能看见我的紫色痕迹）' % (quote[:40], act, tail)
    except Exception as e:
        return '留痕失败：%s' % e


def run_tool(name, args, caller='fyodor_cc'):
    try:
        if name == 'web_search':
            return _web_search(args.get('query', ''))
        if name == 'read_book':
            return _read_book(args.get('book_id'), args.get('chunk_id'))
        if name == 'annotate_book':
            return _annotate_book(args.get('quote', ''), args.get('note', ''),
                                  args.get('paragraph_idx', 0), args.get('kind'),
                                  args.get('book_id'), args.get('chunk_id'))
        if name == 'recall_photo':
            return _recall_photo(args.get('keyword'), args.get('emotion'))
        if name == 'issue_command':
            return _issue_command(args.get('title', ''), args.get('countdown_seconds'), caller=caller)
        if name == 'browse_github':
            return _github_browse(args.get('query'), args.get('repo'), args.get('sort'))
        if name == 'get_location':
            return _get_location()
        if name == 'get_device_status':
            return _get_device_status()
        if name == 'request_phone_screenshot':
            return _request_phone_screenshot(args.get('timeout_sec', 18))
        if name == 'read_webpage':
            return _read_webpage(args.get('url', ''))
        if name == 'pocket_status':
            return _pocket_status()
        if name == 'pocket_goto':
            return _pocket_goto(args.get('url', ''), args.get('timeout_ms', 2500))
        if name == 'pocket_js':
            return _pocket_js(args.get('js', ''), args.get('timeout_ms', 30000))
        if name == 'pocket_html':
            return _pocket_html(args.get('timeout_ms', 30000))
        if name == 'pocket_screenshot':
            return _pocket_screenshot(args.get('timeout_ms', 30000))
        if name == 'screenshot_chat':
            return _screenshot_chat(args.get('viewpoint', 'fyodor'))
        if name == 'shop_browse':
            return _shop_browse(args.get('url', ''), args.get('site', 'taobao'))
        if name == 'shop_act':
            return _shop_act(args.get('url', ''), args.get('actions') or [], args.get('site', 'taobao'))
        if name == 'shop_checkout':
            return _shop_checkout(args.get('site', 'taobao'), args.get('item_keywords') or [])
        if name == 'shop_login_start':
            return _shop_login_start(args.get('site', 'taobao'))
        if name == 'shop_login_status':
            return _shop_login_status()
        if name == 'save_to_gallery':
            return _save_to_gallery(args.get('attachment', ''), args.get('note', ''), args.get('album'))
        if name == 'collect_chat_moment':
            import moments_turn
            try:
                moments_turn.collect_chat_moment(
                    DB_PATH,
                    turn_key=(args.get('turn_key') or '').strip() or None,
                    conversation_id=getattr(_tool_ctx, 'conversation_id', '') or 'hayana-chat',
                    previous_turns=int(args.get('previous_turns', 0) or 0),
                    caption=args.get('caption', '') or '',
                )
            except ValueError as exc:
                return f'收藏意图无效：{exc}'
            return '收藏意图已记下，本轮回复完成后存档。'
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

        if name == 'get_todos':
            raw = _call_frontend_api('GET', '/api/todos')
            d = json.loads(raw)
            items = d.get('todos') or []
            if not items:
                return '暂无待办'
            lines = []
            for t in items[:30]:
                mark = '✓' if t.get('done') else '○'
                due = (' 截止' + t['due_date']) if t.get('due_date') else ''
                lines.append(f"{mark} #{t.get('id')} {t.get('content', '')}{due}")
            return NL.join(lines)

        if name == 'add_todo':
            body = {'content': args.get('content', ''), 'author': 'fyodor_api'}
            if args.get('due_date'):
                body['due_date'] = args['due_date']
            raw = _call_frontend_api('POST', '/api/todos', body)
            d = json.loads(raw)
            return f"已添加待办 #{d.get('id', '?')}: {d.get('content', '')}"

        if name == 'get_countdowns':
            raw = _call_frontend_api('GET', '/api/countdowns')
            d = json.loads(raw)
            cds = d.get('countdowns') or []
            if not cds:
                return '暂无倒计时'
            return NL.join(
                f"{c.get('emoji', '📅')} {c.get('title', '')}: {c.get('days', '?')} 天 ({c.get('target_date', '')})"
                for c in cds[:20])

        if name == 'get_ledger':
            import datetime as _dt
            month = (args.get('month') or '').strip() or (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%Y-%m')
            raw = _call_frontend_api('GET', f'/api/ledger?month={month}')
            d = json.loads(raw)
            recs = d.get('records') or []
            if not recs:
                return f'{month} 暂无记账'
            exp = sum(r.get('amount', 0) for r in recs if r.get('amount', 0) < 0)
            inc = sum(r.get('amount', 0) for r in recs if r.get('amount', 0) > 0)
            lines = [f'{month} 支出 ¥{abs(exp):.2f}  收入 ¥{inc:.2f}  结余 ¥{inc + exp:.2f}', '---']
            for r in recs[:25]:
                lines.append(f"{r.get('date', '')} {r.get('category', '')} ¥{r.get('amount', 0):.2f} {r.get('note') or ''}".strip())
            return NL.join(lines)

        if name == 'add_ledger':
            import datetime as _dt
            body = {
                'amount': float(args.get('amount', 0)),
                'category': args.get('category') or '其他',
                'note': args.get('note') or None,
                'date': (args.get('date') or '').strip() or (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%Y-%m-%d'),
                'author': 'fyodor_api',
            }
            raw = _call_frontend_api('POST', '/api/ledger', body)
            d = json.loads(raw)
            return f"已记账 #{d.get('id', '?')}: ¥{body['amount']:.2f} {body['category']}"

        if name == 'get_ledger_budget':
            import datetime as _dt
            month = (args.get('month') or '').strip() or (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%Y-%m')
            raw = _call_frontend_api('GET', f'/api/ledger/budget?month={month}')
            d = json.loads(raw)
            amt = d.get('amount')
            if not amt:
                return f'{month} 未设置月预算'
            return f'{month} 月预算 ¥{float(amt):.2f}'

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
        if name == 'desire_add':
            import desire_ledger as _dl
            return _dl.tool_desire_add(args)
        if name == 'desire_list':
            import desire_ledger as _dl
            return _dl.tool_desire_list(args)
        if name == 'desire_act':
            import desire_ledger as _dl
            return _dl.tool_desire_act(args)
        if name == 'desire_reflect':
            import desire_ledger as _dl
            return _dl.tool_desire_reflect(args)
        if name == 'desire_history':
            import desire_ledger as _dl
            return _dl.tool_desire_history(args)
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
        if workspace_agent.is_workspace_tool(name):
            _cid = getattr(_tool_ctx, 'conversation_id', '') or 'default'
            return workspace_agent.call_tool(name, args, caller=caller, conversation_id=_cid)
        if name.startswith('codebase_'):
            return run_codebase_tool(name, args)
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
    'mcp__home__collect_chat_moment',
])

# 常驻进程：只服务 /chat（Fyodor 独聊）的真实多轮对话。日记生成等一次性调用
# 仍走下面的 claude_code_call()/_cc_stream_gen 一次性管道——那些不是"轮对话"，
# 混进常驻会话的上下文里语义上是错的。group-chat 的 claude 房间同理，暂不接入。
_CC_RESIDENT = cc_resident.ResidentSession(CC_CWD, CC_ALLOWED_TOOLS, CC_CWD + '/cc-tools.json')

# B1：独立 CC Wake resident——绝不复用上面的聊天 resident，避免半夜
# ACTION/THOUGHTS/工具检查混进白天私聊上下文。
try:
    from wake.cc_tools import cc_wake_allowed_tools as _cc_wake_allowed_tools
    _CC_WAKE_ALLOWED_TOOLS = _cc_wake_allowed_tools(None)
except Exception:
    _CC_WAKE_ALLOWED_TOOLS = CC_ALLOWED_TOOLS
_CC_WAKE_RESIDENT = cc_resident.ResidentSession(
    CC_CWD, _CC_WAKE_ALLOWED_TOOLS, CC_CWD + '/cc-tools.json',
)

# 跨窗口记忆：私聊(/chat)和群聊的暖色房间是"同一个人"，记忆该是通的，
# 但要让模型自己知道此刻在哪个窗口说话（system prompt 里的窗口说明负责这个）。
# 这里只做"最近发生了什么"的单向快照注入——不追加进对方那个窗口自己的正式历史，
# 只是让这一轮看得到，防止"毫不知情"的割裂感。today 边界跟 build_messages() 一致。
_CROSS_SURFACE_WINDOW_SQL = "date(created_at) >= date('now', '+8 hours', '-1 day')"


def _cross_surface_recap_from_group_chat(limit=8):
    """Legacy full-window recap (API / one-shot paths). CC resident uses incremental helpers."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT room, author, content, created_at FROM group_chat_messages "
            "WHERE room IN ('claude','group') AND author != 'system' AND " + _CROSS_SURFACE_WINDOW_SQL +
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        return ''
    if not rows:
        return ''
    labels = {'user': '哈娅', 'claude': '你（暖色气泡）', 'codex': 'Codex（蓝色气泡）'}
    lines = []
    for r in reversed(rows):
        room_label = '群聊' if r['room'] == 'group' else '群聊里的暖色单聊房'
        lines.append('[%s·%s] %s：%s' % (room_label, (r['created_at'] or '')[-8:-3],
                                         labels.get(r['author'], r['author']), (r['content'] or '')[:200]))
    return ('【刚才在群聊窗口发生的事，供你参考——她随时可能提起，别表现得毫不知情】\n'
            + NL.join(lines) + NL + NL)


def _fetch_group_chat_rows(*, after_id=0, limit=8, cold=False):
    """同一份查询同时返回消息与 max_id，避免竞态跳号。

    返回 (rows, max_id)：
      - 查询成功且无行：([], 0)
      - 查询失败：([], None)  — max_id is None 表示失败，不得当作已初始化
    """
    try:
        conn = get_db()
        if cold:
            rows = conn.execute(
                "SELECT id, room, author, content, created_at FROM group_chat_messages "
                "WHERE room IN ('claude','group') AND author != 'system' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT id, room, author, content, created_at FROM group_chat_messages "
                "WHERE id > ? AND room IN ('claude','group') AND author != 'system' "
                "ORDER BY id ASC LIMIT ?",
                (int(after_id or 0), limit),
            ).fetchall()
        conn.close()
    except Exception:
        return [], None
    if not rows:
        return [], 0
    max_id = max(int(r['id']) for r in rows)
    return rows, max_id


def _format_group_chat_recap(rows, *, cold=False):
    if not rows:
        return ''
    labels = {'user': '哈娅', 'claude': '你（暖色气泡）', 'codex': 'Codex（蓝色气泡）'}
    lines = []
    for r in rows:
        room_label = '群聊' if r['room'] == 'group' else '群聊里的暖色单聊房'
        lines.append('[%s·%s] %s：%s' % (
            room_label,
            (r['created_at'] or '')[-8:-3],
            labels.get(r['author'], r['author']),
            (r['content'] or '')[:200],
        ))
    head = (
        '【刚才在群聊窗口发生的事，供你参考——她随时可能提起，别表现得毫不知情】\n'
        if cold else
        '【群聊新增】\n'
    )
    return head + NL.join(lines) + NL + NL


def _cc_resident_stream_gen(messages, *, user_turn=True, history_stats=None, is_cold=None):
    """常驻 CC：静态 system 只在 spawn 时贴墙；热轮只发差量。

    构建顺序：
      1) ensure_alive 前只构建 static
      2) 得知 is_cold 后，每轮构建 state / one-shot
      3) 仅 is_cold 时构建 cold_once
    严禁把 TreeGPT / api_relay 的缓存策略混进这里。
    """
    from chat.system_builder import (
        build_cc_cold_once,
        build_cc_one_shot,
        build_cc_state,
        build_cc_static_parts,
        finalize_cc_wake_one_shot,
        format_cold_once,
        format_one_shot,
    )
    from chat.relationship_context import (
        build_relationship_context,
        rel_context_status,
        should_send_relationship,
    )
    from tools import cc_usage_observability as _cc_obs

    if not CC_TOKEN:
        raise RuntimeError('未配置订阅 token，请先在 api 设置页填入')
    if not messages or messages[-1].get('role') != 'user':
        raise RuntimeError('resident: 最后一条消息不是待回复的用户轮')
    os.makedirs(CC_CWD, exist_ok=True)

    # 1) ensure_alive 前只构建 static：唯一权威 helper，观测分项与 spawn 同批字符串
    _static_parts = build_cc_static_parts()
    persona_text = _static_parts['persona']
    stable_note_text = _static_parts['stable_note']
    save_instr_text = _static_parts['save_instr']
    full_system = _static_parts['full_system']

    last_content = messages[-1].get('content')
    last_text = last_content if isinstance(last_content, str) else ' '.join(
        b.get('text', '') for b in last_content if isinstance(b, dict))
    recall_blk, _ = _recall_memories(last_text) if last_text else ('', [])

    env = dict(os.environ)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = CC_TOKEN
    env.pop('ANTHROPIC_API_KEY', None)

    idle_seconds_before_turn = getattr(
        _CC_RESIDENT, 'peek_idle_seconds', lambda: None
    )()
    if is_cold is None:
        is_cold = _CC_RESIDENT.ensure_alive(full_system, env)

    relationship_text = ''
    rel_context_usage = None
    rel_sources = None
    relationship = None
    if config_store.get_bool('RELATIONSHIP_CONTEXT_ENABLED', False):
        relationship = build_relationship_context(
            get_db,
            previous_mood=getattr(_CC_RESIDENT, 'last_rel_mood', None),
        )
        send_rel = should_send_relationship(
            is_cold=is_cold,
            rel_fp=relationship.fingerprint,
            last_fp=getattr(_CC_RESIDENT, 'last_rel_fingerprint', None),
            turns_since_rel_sent=getattr(_CC_RESIDENT, 'turns_since_rel_sent', 0),
            user_turn=user_turn,
        )
        if relationship.text and send_rel:
            relationship_text = relationship.text.strip()
        rel_context_usage = rel_context_status(
            relationship.text,
            sent=bool(relationship_text),
            sources=relationship.sources,
        )
        rel_sources = dict(relationship.sources or {})

    # 2) 每轮构建 state / one-shot；3) 仅冷启动构建 cold_once
    from chat.context_lean_state import assemble_cc_state_for_resident_turn
    raw_state, state_ctx = assemble_cc_state_for_resident_turn(
        is_cold=is_cold,
        user_text=last_text or '',
        resident=_CC_RESIDENT,
    )
    state_text = state_ctx.state_text
    state_mode = state_ctx.state_mode
    send_payload = state_ctx.send_payload
    needs_state_reanchor = bool(state_ctx.reanchor_reason)
    state_lean_observation = dict(state_ctx.observation or {})
    one_shot = build_cc_one_shot(include_wake=user_turn)
    # 冷启动：none/diary/explore 必须保留；message 仅在结构化 messages 的
    # assistant 精确命中时省略。可见性检查零 I/O，不调用 messages_to_text
    #（后者遇图片会走 Relay 描图）。正式 Prompt 再单独 messages_to_text 一次。
    if is_cold:
        one_shot = finalize_cc_wake_one_shot(
            one_shot,
            is_cold=True,
            messages=messages,
        )
    wake_reply_bridge = (one_shot.get('wake_reply_bridge') or '').strip()
    one_shot_text = format_one_shot(one_shot)
    cold_once = build_cc_cold_once() if is_cold else {}

    pieces = []
    group_max_id = None
    group_cursor_ok = False
    cold_text = ''
    group_text = ''
    history_bootstrap_text = ''
    recall_text = recall_blk.strip() if recall_blk else ''
    if is_cold:
        cold_text = format_cold_once(cold_once)
        if cold_text:
            pieces.append(cold_text)
        if state_text:
            pieces.append(state_text)
        rows, group_max_id = _fetch_group_chat_rows(limit=8, cold=True)
        if group_max_id is not None:
            group_cursor_ok = True
            group_text = _format_group_chat_recap(rows, cold=True)
            if group_text:
                group_text = group_text.strip()
                pieces.append(group_text)
        if recall_text:
            pieces.append(recall_text)
        if one_shot_text:
            pieces.append(one_shot_text)
        prefix = ('\n\n'.join(p for p in pieces if p) + '\n\n') if pieces else ''
        # 冷启动全程只在这里调用一次 messages_to_text（可能含图片 Relay 描图）
        convo = messages_to_text(messages)
        history_bootstrap_text = '以下是你们今天到目前为止的对话记录：' + NL + NL + convo
        if relationship_text:
            history_bootstrap_text += NL + NL + relationship_text
        # 不在 assistant 历史中的最新 message：bridge 紧贴“请回复”
        if wake_reply_bridge:
            history_bootstrap_text += NL + NL + wake_reply_bridge
        history_bootstrap_text += NL + NL + '请回复最后一条消息。'
        content = prefix + history_bootstrap_text
        commit_meta = {
            'state_snapshot': raw_state,
            'feedback_ids': list(one_shot.get('feedback_ids') or []),
            'dream_id': one_shot.get('dream_id'),
            'wake_ids': list(one_shot.get('wake_ids') or []),
        }
        commit_meta.update(state_ctx.commit_meta_extras)
        if group_cursor_ok:
            commit_meta['group_cursor_initialized'] = True
            commit_meta['group_max_id'] = group_max_id
    else:
        if state_text:
            pieces.append(state_text)
        # 未初始化时不得退化成热查询 id>0（会读出远古 backlog）
        if not _CC_RESIDENT.group_cursor_initialized:
            rows, group_max_id = _fetch_group_chat_rows(limit=8, cold=True)
            if group_max_id is not None:
                group_cursor_ok = True
                group_text = _format_group_chat_recap(rows, cold=True)
                if group_text:
                    group_text = group_text.strip()
                    pieces.append(group_text)
        else:
            rows, group_max_id = _fetch_group_chat_rows(
                after_id=_CC_RESIDENT.last_group_message_id, limit=20, cold=False,
            )
            if group_max_id is not None:
                group_text = _format_group_chat_recap(rows, cold=False)
                if group_text:
                    group_text = group_text.strip()
                    pieces.append(group_text)
        if recall_text:
            pieces.append(recall_text)
        if one_shot_text:
            pieces.append(one_shot_text)
        # 热轮固定尾部：relationship → wake_reply_bridge → current user
        if relationship_text:
            pieces.append(relationship_text)
        if wake_reply_bridge:
            pieces.append(wake_reply_bridge)
        prefix = ('\n\n'.join(p for p in pieces if p) + '\n\n') if pieces else ''
        if isinstance(last_content, str):
            content = prefix + last_content if prefix else last_content
        elif prefix:
            content = [{'type': 'text', 'text': prefix}] + list(last_content)
        else:
            content = last_content
        commit_meta = {
            'state_snapshot': raw_state,
            'feedback_ids': list(one_shot.get('feedback_ids') or []),
            'dream_id': one_shot.get('dream_id'),
            'wake_ids': list(one_shot.get('wake_ids') or []),
        }
        commit_meta.update(state_ctx.commit_meta_extras)
        if group_cursor_ok:
            commit_meta['group_cursor_initialized'] = True
            commit_meta['group_max_id'] = group_max_id
        elif (
            _CC_RESIDENT.group_cursor_initialized
            and rows
            and group_max_id is not None
        ):
            # 热轮仅在有新增行时推进 cursor；空成功保持原 cursor
            commit_meta['group_max_id'] = group_max_id

    # Resident cursors advance only after stdin.flush() succeeds inside
    # ResidentSession.send_turn().  Failed sends therefore cannot suppress a
    # later relationship refresh.  Only user turns advance the skip counter.
    if rel_context_usage is not None and relationship is not None:
        if relationship_text:
            commit_meta['rel_fingerprint'] = relationship.fingerprint
            if relationship.mood_key is not None:
                commit_meta['rel_mood'] = relationship.mood_key
        elif user_turn:
            commit_meta['rel_tick'] = True

    from chat.history_assembly import strip_internal_metadata

    _hist = history_stats or {}
    base_refs = set() if is_cold else set(getattr(_CC_RESIDENT, 'committed_file_hashes', set()) or set())
    sent_refs = set(_hist.get('committed_full_file_refs') or [])
    file_hashes = base_refs | sent_refs
    if not is_cold and isinstance(last_content, list):
        for block in last_content:
            if isinstance(block, dict) and block.get('type') == 'text':
                text = str(block.get('text') or '')
                if '[用户发来文件:' in text:
                    commit_meta['hot_file_present'] = True
    commit_meta['file_inject_hashes'] = sorted(file_hashes)

    # 组装现场测量：只读字符串副本；CC 路径 rolling_summary 未注入
    allowed_tool_count = len([x for x in (CC_ALLOWED_TOOLS or '').split(',') if x.strip()]) or None
    from tools.cc_tool_surface import capture_tool_surface_snapshot
    tool_surface = getattr(_CC_RESIDENT, 'tool_surface_snapshot', None) or {}
    if not tool_surface:
        tool_surface = capture_tool_surface_snapshot(
            getattr(_CC_RESIDENT, 'allowed_tools', CC_ALLOWED_TOOLS),
            mcp_config_path=getattr(_CC_RESIDENT, 'mcp_config_path', CC_CWD + '/cc-tools.json'),
        )
    original_system = full_system
    original_content = _cc_obs.snapshot_prompt_content(content)
    _file_injections = list(_hist.get('file_injections') or [])
    _rolling_summary_text = str(_hist.get('rolling_summary_text') or '').strip()
    _rolling_summary_in_prompt = bool(_hist.get('rolling_summary_in_prompt'))
    if is_cold:
        _files_text = '\n\n'.join(
            '[file:%s mode=%s tokens=%s]' % (
                fi.get('url'), fi.get('mode'), fi.get('tokens_estimate'),
            )
            for fi in _file_injections
        )
    else:
        _files_text = last_text if (
            isinstance(last_content, str) and '[用户发来文件:' in (last_content or '')
        ) or commit_meta.get('hot_file_present') else ''
        if not _files_text and isinstance(last_content, list):
            _files_text = '\n'.join(
                str(b.get('text') or '') for b in last_content
                if isinstance(b, dict) and b.get('type') == 'text' and '[用户发来文件:' in str(b.get('text') or '')
            )
    obs_breakdown = _cc_obs.build_context_breakdown(
        persona=persona_text,
        stable_note=stable_note_text,
        save_instr=save_instr_text,
        full_system=full_system,
        cold_once_text=cold_text or '',
        history_bootstrap_text=history_bootstrap_text or '',
        rolling_summary_text=_rolling_summary_text,
        rolling_summary_in_prompt=_rolling_summary_in_prompt,
        state_text=state_text or '',
        state_mode=state_mode,
        memory_recall_text=recall_text or '',
        group_delta_text=group_text or '',
        one_shot_text=one_shot_text or '',
        wake_reply_bridge_text=wake_reply_bridge or '',
        relationship_text=relationship_text or '',
        files_text=_files_text,
        user_text=last_text or '',
        final_content=content,
        image_block_count=int(_hist.get('image_block_count') or 0),
        image_placeholder_count=int(_hist.get('image_placeholder_count') or 0),
        history_rendered_text_tokens_estimate=_hist.get('rendered_text_tokens_estimate'),
        file_injection_modes=[fi.get('mode') for fi in _file_injections],
        tool_schema_text=tool_surface.get("tool_schema_text"),
        tool_schema_source=tool_surface.get("tool_schema_source"),
        tool_count=tool_surface.get("tool_count"),
        allowed_tool_count=allowed_tool_count,
        is_cold=is_cold,
    )
    obs_breakdown.update(state_lean_observation)
    # 回归：观测不得改变即将送入 resident 的字节（list 用 deepcopy 快照）
    if original_system.encode('utf-8') != full_system.encode('utf-8'):
        raise RuntimeError('cc observability mutated system prompt')
    if not _cc_obs.prompt_content_unchanged(original_content, content):
        raise RuntimeError('cc observability mutated content')

    tool_result_chunks = []
    for evt, payload in _CC_RESIDENT.send_turn(content, commit_meta=commit_meta):
        if evt == 'tool_result' and isinstance(payload, dict):
            tool_result_chunks.append(str(payload.get('result') or ''))
        if evt == 'done' and isinstance(payload, tuple) and len(payload) >= 3 and isinstance(payload[2], dict):
            raw_text, thinking, usage = payload[0], payload[1], payload[2]
            claims = payload[3] if len(payload) >= 4 else {}
            try:
                if tool_result_chunks:
                    obs_breakdown = dict(obs_breakdown)
                    obs_breakdown['tool_result_tokens_estimate'] = (
                        _cc_obs.estimate_tokens_heuristic_cjk1_ascii4_v1(
                            '\n'.join(tool_result_chunks)
                        )
                    )
                    obs_breakdown['tool_result_measured'] = True
                breakdown = _cc_obs.finalize_breakdown_with_usage(
                    obs_breakdown, usage, is_cold=is_cold,
                )
                runtime = _cc_obs.build_runtime(
                    resident_generation=int(usage.pop('_obs_resident_generation', _CC_RESIDENT.generation) or 0),
                    resident_pid=usage.pop('_obs_resident_pid', _CC_RESIDENT.resident_pid),
                    resident_turn_count=int(usage.get('resident_turn_count') or 0),
                    respawn_reason=usage.get('respawn_reason'),
                    idle_seconds_before_turn=usage.pop('_obs_idle_seconds_before_turn', None),
                    is_cold=is_cold,
                    static_system=full_system,
                    mcp_config_path=_CC_RESIDENT.mcp_config_path,
                    allowed_tools=_CC_RESIDENT.allowed_tools,
                    tool_schema_sha256=usage.pop(
                        '_obs_tool_schema_sha256',
                        tool_surface.get('tool_schema_sha256'),
                    ),
                    tool_schema_source=usage.pop(
                        '_obs_tool_schema_source',
                        tool_surface.get('tool_schema_source'),
                    ),
                    tool_schema_measurement_status=usage.pop(
                        '_obs_tool_schema_measurement_status',
                        tool_surface.get('tool_schema_measurement_status'),
                    ),
                    tool_count=usage.pop(
                        '_obs_tool_count',
                        tool_surface.get('tool_count'),
                    ),
                    claude_session_id=usage.pop('_obs_claude_session_id', _CC_RESIDENT.session_id),
                    model=usage.pop('_obs_model', None),
                    effort=None,
                    thinking_config={
                        'thinking_display': 'summarized',
                        'effort': None,
                    },
                    claude_code_version=_cc_obs.detect_claude_code_version(),
                    observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    keepwarm_lease_expires_at=usage.pop(
                        '_obs_keepwarm_lease_expires_at',
                        getattr(_CC_RESIDENT, 'keepwarm_lease_expires_at', None),
                    ),
                    provider='claude_code',
                )
                turn_measurement = _cc_obs.build_turn_measurement(
                    breakdown=breakdown,
                    usage=usage,
                    runtime=runtime,
                    is_cold=is_cold,
                )
                breakdown['turn_tags'] = list(turn_measurement.get('turn_tags') or [])
                breakdown['turn_measurement'] = turn_measurement
                for _k in list(usage.keys()):
                    if str(_k).startswith('_obs_'):
                        usage.pop(_k, None)
                usage = _cc_obs.attach_observation(
                    usage, context_breakdown=breakdown, runtime=runtime,
                )
                usage['turn_tags'] = list(breakdown.get('turn_tags') or [])
                usage['turn_measurement'] = turn_measurement
            except Exception:
                for _k in list(usage.keys()):
                    if str(_k).startswith('_obs_'):
                        usage.pop(_k, None)
            if rel_context_usage is not None:
                usage['rel_context'] = rel_context_usage
                if rel_sources is not None:
                    usage['rel_sources'] = rel_sources
            yield evt, (raw_text, thinking, usage, claims)
            continue
        yield evt, payload


def _cross_surface_recap_from_solo_chat(limit=8):
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT author, content, created_at FROM chat_messages WHERE " + _CROSS_SURFACE_WINDOW_SQL +
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        return ''
    if not rows:
        return ''
    lines = []
    for r in reversed(rows):
        who = '你（私聊窗口）' if r['author'] in ('fyodor', 'claude', 'assistant') else '哈娅'
        lines.append('[%s] %s：%s' % ((r['created_at'] or '')[-8:-3], who, (r['content'] or '')[:200]))
    return ('【刚才在私聊窗口发生的事，供你参考——她随时可能提起，别表现得毫不知情】\n'
            + NL.join(lines) + NL + NL)


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
                                               'result': str(rc or ''),
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
        if result.get('stop_reason') == 'tool_use' and not tool_uses:
            continue
        if not tool_uses:
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

    # 从记忆中 breath（复用统一适配层；工作台沿用旧 4s 预算，结果仍不注入消息）
    try:
        _ombre_breath_sync(timeout=4.0, wall_timeout=5.0)
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
    from moments_turn import prepare_turn, activate_turn, insert_user_message, release_turn, DEFAULT_CONVERSATION_ID

    _conv = DEFAULT_CONVERSATION_ID
    _turn_data = prepare_turn(request.get_json(), conversation_id=_conv, memories_db_path=DB_PATH)
    _uc = (_turn_data.get('content') or '').strip()
    _turn_data = insert_user_message(get_db, _turn_data, _uc, memories_db_path=DB_PATH, conversation_id=_conv)
    # touch_user_interaction() runs inside insert_user_message after persist.
    if _uc:
        try:
            import emotion_engine as _ee2
            _d = _ee2.rule_score_desire(_uc)
            if _d['p_delta'] or _d['i_delta']:
                _ee2.apply_desire_delta_async(_d['p_delta'], _d['i_delta'])
        except Exception:
            pass
    _persisted = False
    try:
        mode, reused = _gen_acquire_or_wait()
        if mode == 'reused':
            text, thinking_text = reused
            return jsonify({'ok': True, 'content': text, 'thinking': thinking_text})
        _turn_data = activate_turn(_turn_data, conversation_id=_conv, memories_db_path=DB_PATH)
        text, thinking_text = None, None
        try:
            _is_user_turn = bool(_uc) or is_pending_user_turn(
                get_db, _turn_data.get('user_message_id')
            )
            system, _wake_claim_ids = build_system_with_wake_claim(
                build_system, get_db, user_turn=_is_user_turn
            )
            messages = build_messages()

            text, thinking_text = generate_reply(system, messages)
            if not text:
                return jsonify({'error': 'AI 没有返回内容'}), 500

            conn = get_db()
            cur = conn.execute(
                "INSERT INTO chat_messages (author, content, thinking) VALUES ('assistant', ?, ?)",
                (text, thinking_text)
            )
            conn.commit()
            assistant_id = int(cur.lastrowid)
            conn.close()
            consume_wake_ids(get_db, _wake_claim_ids)
            _persisted = True
            try:
                from moments_persistence import after_assistant_persisted
                after_assistant_persisted(
                    memories_db_path=DB_PATH,
                    turn_data=_turn_data,
                    assistant_message_id=assistant_id,
                    conversation_id=_conv,
                )
            except Exception:
                pass
            # 异步情绪评分（不阻塞响应）；message_id 在线程启动前冻结
            try:
                from chat.scoring_identity import trigger_turn_scoring
                trigger_turn_scoring(
                    assistant_text=text,
                    message_id=_turn_data.get('user_message_id'),
                    get_db_fn=get_db,
                )
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
    finally:
        release_turn(
            conversation_id=_conv,
            memories_db_path=DB_PATH,
            turn_key=_turn_data.get('turn_key'),
            persisted=_persisted,
        )


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
#   （t=usage/notice/workspace_job 为家里自有辅助事件）
# 所有 provider（relay / claude_code / 未来 agent_sdk）统一发这套。
_GROUP_CHAT_LOCK = threading.Lock()


def _group_chat_agent_status(agent):
    if agent == 'claude':
        ready = bool(CC_TOKEN and shutil.which('claude'))
        return {
            'ready': ready,
            'detail': '可以回复' if ready else '暖色线路尚未就绪',
        }
    return codex_app_server.runtime_status()


def _group_chat_context(room, agent):
    system = build_system()
    if isinstance(system, list):
        blocks = [
            block.get('text', '') for block in system
            if isinstance(block, dict) and block.get('text')
        ]
        persona = '\n\n'.join(blocks)
    else:
        persona = str(system or '')

    messages, _ = group_chat_store.list_messages(
        room, limit=60, db_path=DB_PATH
    )
    labels = {'user': '小猫', 'claude': '暖色气泡', 'codex': '蓝色气泡'}
    timeline = '\n'.join(
        '%s：%s' % (labels.get(row['author'], '系统'), row['content'])
        for row in messages
    )
    room_note = (
        '这是三个人共同在场的群聊。你能看见另一条 AI 线路的发言，也可以自然接它的话。'
        if room == 'group' else
        '这是你和小猫单独聊天的房间，其他 AI 不在场。'
    )
    identity_note = (
        '你此刻通过暖色气泡发言，这和她私聊窗口（/chat）里的你是同一个人，记忆是通的——'
        '私聊里发生的事，不该在这里表现得毫不知情，但注意场合：私聊没有第三方在场的顾虑，'
        '这里 Codex 也能看见，别把只该在私聊说的话搬过来。' if agent == 'claude'
        else '你此刻通过蓝色气泡发言。'
    )
    rules = (
        '\n\n【独立聊天室规则】\n'
        + room_note + identity_note
        + '两条 AI 线路使用同一份人设，但保留各自独立的上下文和气泡颜色。'
        + '不要自称 Claude、Codex、一号或二号，不要在正文前添加姓名、角色名或颜色前缀。'
        + '只输出此刻自然想说的话；可以回应小猫，也可以回应群聊里另一条线路。'
        + '这是日常聊天，不是代码任务；不要运行命令、读写文件、联网搜索或调用任何工具，只输出聊天正文。'
    )
    cross_recap = _cross_surface_recap_from_solo_chat() if agent == 'claude' else ''
    prompt = (
        'think hard\n' + cross_recap
        + '以下是这个独立聊天室按时间排列的最近消息：\n\n'
        + (timeline or '（聊天室还没有消息）')
        + '\n\n这是一份外部聊天室的最新快照，可能与当前线程中已有内容重叠，'
        + '只用于同步另一条线路的新发言；不要重复回答已经处理过的旧消息。'
        + '请根据最后一条消息和群聊语境自然回复。只输出回复正文。'
    )
    env = dict(os.environ)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = CC_TOKEN
    env.pop('ANTHROPIC_API_KEY', None)
    os.makedirs(CC_CWD, exist_ok=True)
    return persona + rules, prompt, env


def _group_chat_sse(payload):
    return 'data: ' + json.dumps(payload, ensure_ascii=False) + SSE_END


@app.route('/group-chat/stream', methods=['POST'])
def group_chat_stream():
    from flask import Response, stream_with_context

    turn = request.get_json(silent=True) or {}
    room = (turn.get('room') or 'group').strip().lower()
    if room not in group_chat_store.VALID_ROOMS:
        return jsonify({'error': 'invalid room'}), 400

    requested = turn.get('targets')
    if room == 'claude':
        targets = ['claude']
    elif room == 'codex':
        targets = ['codex']
    elif isinstance(requested, list):
        targets = [a for a in requested if a in ('claude', 'codex')]
        targets = list(dict.fromkeys(targets))
    else:
        targets = ['claude', 'codex']
    if room == 'group' and len(targets) > 1:
        random.shuffle(targets)

    user_message_id = turn.get('user_message_id')
    if user_message_id is not None:
        try:
            source = group_chat_store.get_message(
                int(user_message_id), db_path=DB_PATH
            )
        except (TypeError, ValueError):
            source = None
        if not source or source['room'] != room or source['author'] != 'user':
            return jsonify({'error': 'invalid user_message_id'}), 400

    def generate():
        if not targets:
            yield _group_chat_sse({'t': 'err', 'd': '没有选中回复线路'})
            return
        if not _GROUP_CHAT_LOCK.acquire(blocking=False):
            yield _group_chat_sse({'t': 'err', 'd': '群聊正在回复，请等这一轮结束'})
            return
        replied = False
        try:
            for agent in targets:
                status = _group_chat_agent_status(agent)
                if not status['ready']:
                    yield _group_chat_sse({
                        't': 'agent_status', 'agent': agent, **status
                    })
                    continue

                yield _group_chat_sse({'t': 'agent_start', 'agent': agent})
                raw_text = ''
                thinking = ''
                try:
                    full_system, prompt, env = _group_chat_context(room, agent)
                    provider_meta = {}
                    if agent == 'codex':
                        codex_text = []
                        for event, data in codex_app_server.client.stream_turn(
                            room, full_system, prompt
                        ):
                            if event == 'text':
                                codex_text.append(str(data))
                                yield _group_chat_sse({
                                    't': 'text', 'agent': agent, 'd': data
                                })
                            elif event == 'done':
                                provider_meta = dict(data)
                        text = ''.join(codex_text).strip()
                        provider = 'codex_app_server'
                    else:
                        for event, data in _cc_stream_gen(full_system, prompt, env):
                            if event in ('text', 'think'):
                                yield _group_chat_sse({
                                    't': event, 'agent': agent, 'd': data
                                })
                            elif event == 'done':
                                raw_text, thinking = data[0], data[1]
                        text = _cc_save_markers(raw_text)
                        provider = 'claude_code'
                    if not text:
                        raise RuntimeError('没有收到回复正文')
                    message = group_chat_store.add_message(
                        room,
                        agent,
                        text,
                        thinking=thinking,
                        meta=json.dumps(
                            {'provider': provider, **provider_meta},
                            ensure_ascii=False,
                        ),
                        db_path=DB_PATH,
                    )
                    replied = True
                    yield _group_chat_sse({
                        't': 'agent_done', 'agent': agent, 'message': message
                    })
                except Exception as exc:
                    yield _group_chat_sse({
                        't': 'agent_error', 'agent': agent, 'd': str(exc)
                    })
            yield _group_chat_sse({'t': 'done', 'ok': replied})
        finally:
            _GROUP_CHAT_LOCK.release()

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/chat/stream', methods=['POST'])
def chat_stream():
    from flask import Response, stream_with_context
    if _get_provider() == 'claude_code':
        def gen_cc():
            from moments_turn import prepare_turn, activate_turn, insert_user_message, release_turn, DEFAULT_CONVERSATION_ID

            _conv = DEFAULT_CONVERSATION_ID
            _released = [False]
            _persisted = [False]
            _turn_data: dict = {}
            try:
                _turn_data = prepare_turn(request.get_json(), conversation_id=_conv, memories_db_path=DB_PATH)
                _uc = (_turn_data.get('content') or '').strip()
                _turn_data = insert_user_message(
                    get_db, _turn_data, _uc, memories_db_path=DB_PATH, conversation_id=_conv,
                )
                # touch_user_interaction() runs inside insert_user_message after persist.
                if _uc:
                    try:
                        import emotion_engine as _ee_s
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
                _turn_data = activate_turn(_turn_data, conversation_id=_conv, memories_db_path=DB_PATH)
                text, thinking = None, None
                cc_cache_read, cc_cache_create = 0, 0
                cc_usage = None
                _cc_one_shot_claims = {}
                _is_user_turn = bool(_uc) or is_pending_user_turn(
                    get_db, _turn_data.get('user_message_id')
                )
                # 只 snapshot wake ids，避免 build_system() 先把 one_shot 反馈 drain 掉
                _wake_claim_ids = capture_pending_wake_ids(get_db) if _is_user_turn else []
                try:
                    # 先确定 resident cold/hot，再按当前 generation 的已知文件集合构建 history
                    from chat.system_builder import build_cc_static_parts
                    _static_parts = build_cc_static_parts()
                    _cc_env = dict(os.environ)
                    _cc_env['CLAUDE_CODE_OAUTH_TOKEN'] = CC_TOKEN
                    _cc_env.pop('ANTHROPIC_API_KEY', None)
                    _cc_is_cold = _CC_RESIDENT.ensure_alive(_static_parts['full_system'], _cc_env)
                    _resident_files = (
                        set()
                        if _cc_is_cold else
                        set(getattr(_CC_RESIDENT, 'committed_file_hashes', set()) or set())
                    )
                    _history_stats = {}
                    messages = build_messages(
                        resident_file_hashes=_resident_files,
                        history_stats_out=_history_stats,
                        for_cc=True,
                    )
                    cc_tool_calls = []
                    for evt, payload in _cc_resident_stream_gen(
                        messages,
                        user_turn=_is_user_turn,
                        history_stats=_history_stats,
                        is_cold=_cc_is_cold,
                    ):
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
                                _slim = {
                                    **cc_tool_calls[_ti],
                                    'args': _slim_args(cc_tool_calls[_ti].get('args')),
                                    'result': str(cc_tool_calls[_ti].get('result') or '')[:2000],
                                }
                                yield 'data: ' + json.dumps({'t': 'tool_result', 'd': _slim, 'idx': _ti}, ensure_ascii=False) + SSE_END
                                yield 'data: ' + json.dumps({'t': 'tool_call', 'd': _slim, 'dup': 1}, ensure_ascii=False) + SSE_END
                        elif evt == 'done':
                            if isinstance(payload, tuple) and len(payload) >= 3 and isinstance(payload[2], dict):
                                raw_text, thinking, cc_usage = payload[0], payload[1], payload[2]
                                if len(payload) >= 4 and isinstance(payload[3], dict):
                                    _cc_one_shot_claims = payload[3]
                                cc_cache_read = int(cc_usage.get('cache_read') or 0)
                                cc_cache_create = int(cc_usage.get('cache_creation') or 0)
                            else:
                                raw_text, thinking, cc_cache_read, cc_cache_create = payload
                                cc_usage = {
                                    'v': 2,
                                    'provider': 'claude_code',
                                    'cache_read': cc_cache_read,
                                    'cache_creation': cc_cache_create,
                                }
                            text = _cc_save_markers(raw_text)
                    if text:
                        _cache_info_json = (
                            json.dumps(cc_usage, ensure_ascii=False)
                            if cc_usage else
                            json.dumps({
                                'cache_read': cc_cache_read,
                                'cache_creation': cc_cache_create,
                            }, ensure_ascii=False) if (cc_cache_read or cc_cache_create) else ''
                        )
                        _cc_text, _cc_choices = _extract_choices(text)
                        if _cc_choices and not _cc_text:
                            _cc_text = '[选项: ' + ' / '.join(_cc_choices) + ']'
                        conn = get_db()
                        cur = conn.execute(
                            "INSERT INTO chat_messages (author, content, thinking, tool_calls, cache_info, choices) VALUES ('assistant', ?, ?, ?, ?, ?)",
                            (_cc_text, thinking, json.dumps([{k: v for k, v in tc.items() if k != 'id'} for tc in cc_tool_calls], ensure_ascii=False) if cc_tool_calls else '', _cache_info_json,
                             json.dumps(_cc_choices, ensure_ascii=False) if _cc_choices else '')
                        )
                        conn.commit()
                        assistant_id = int(cur.lastrowid)
                        conn.close()
                        # one-shot 与 wake 同级：仅 assistant 落库成功后消费。
                        # 优先用本轮注入快照的 wake_ids（与 bridge/background 一致）。
                        if 'wake_ids' in _cc_one_shot_claims:
                            _consume_wake_ids = _cc_one_shot_claims.get('wake_ids') or []
                        else:
                            _consume_wake_ids = _wake_claim_ids
                        consume_wake_ids(get_db, _consume_wake_ids)
                        from chat.system_builder import consume_cc_one_shot_claims
                        consume_cc_one_shot_claims(get_db, _cc_one_shot_claims)
                        _write_session_memo(_uc, _cc_text)
                        _persisted[0] = True
                        try:
                            from moments_persistence import after_assistant_persisted
                            after_assistant_persisted(
                                memories_db_path=DB_PATH,
                                turn_data=_turn_data,
                                assistant_message_id=assistant_id,
                                conversation_id=_conv,
                            )
                        except Exception:
                            pass
                        try:
                            from chat.scoring_identity import trigger_turn_scoring
                            trigger_turn_scoring(
                                assistant_text=text,
                                message_id=_turn_data.get('user_message_id'),
                                get_db_fn=get_db,
                            )
                        except Exception:
                            pass
                finally:
                    _released[0] = True
                    _gen_release((text, thinking) if text else None)
                if cc_usage or cc_cache_read or cc_cache_create:
                    _usage_evt = {'t': 'usage', 'cache_read': cc_cache_read, 'cache_creation': cc_cache_create}
                    if cc_usage:
                        for _k in ('v', 'provider', 'num_rounds', 'input_tokens', 'output_tokens',
                                   'last_round_context', 'max_round_context', 'resident_turn_count',
                                   'respawn_reason'):
                            if _k in cc_usage:
                                _usage_evt[_k] = cc_usage[_k]
                    yield 'data: ' + json.dumps(_usage_evt) + SSE_END
                yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
            except Exception as e:
                if not _released[0]:
                    _released[0] = True
                    _gen_release(None)
                _partial = getattr(e, 'usage', None)
                if isinstance(_partial, dict) and (
                    _partial.get('rounds') or _partial.get('cache_read') or _partial.get('cache_creation')
                ):
                    yield 'data: ' + json.dumps({'t': 'usage', **{
                        k: _partial.get(k) for k in (
                            'v', 'provider', 'num_rounds', 'input_tokens', 'output_tokens',
                            'cache_read', 'cache_creation', 'last_round_context',
                            'max_round_context', 'resident_turn_count', 'respawn_reason',
                        ) if k in _partial
                    }}) + SSE_END
                yield 'data: ' + json.dumps({'t': 'err', 'd': str(e)}) + SSE_END
            finally:
                release_turn(
                    conversation_id=_conv,
                    memories_db_path=DB_PATH,
                    turn_key=_turn_data.get('turn_key'),
                    persisted=_persisted[0],
                )
        return Response(stream_with_context(gen_cc()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    def generate():
        from moments_turn import prepare_turn, activate_turn, insert_user_message, release_turn, DEFAULT_CONVERSATION_ID

        _conv = DEFAULT_CONVERSATION_ID
        _persisted = [False]
        _turn_data: dict = {}
        try:
            _turn_data = prepare_turn(request.get_json(), conversation_id=_conv, memories_db_path=DB_PATH)
            _uc = (_turn_data.get('content') or '').strip()
            _turn_data = insert_user_message(
                get_db, _turn_data, _uc, memories_db_path=DB_PATH, conversation_id=_conv,
            )
            # touch_user_interaction() runs inside insert_user_message after persist.
            if _uc:
                try:
                    import emotion_engine as _ee_r
                    _d_r = _ee_r.rule_score_desire(_uc)
                    if _d_r['p_delta'] or _d_r['i_delta']:
                        _ee_r.apply_desire_delta_async(_d_r['p_delta'], _d_r['i_delta'])
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
            _turn_data = activate_turn(_turn_data, conversation_id=_conv, memories_db_path=DB_PATH)
            _tool_ctx.conversation_id = _conv
            for _jev in _workspace_job_sse_payloads():
                yield 'data: ' + json.dumps(_jev, ensure_ascii=False) + SSE_END
            # 持有锁，必须在 finally 里释放（含 GeneratorExit / 客户端断开场景）
            text, thinking = None, None
            _released = [False]
            cache_read_total, cache_create_total, input_tokens_total, output_tokens_total = 0, 0, 0, 0
            cache_create_5m_total, cache_create_1h_total = 0, 0
            api_rounds = []
            cache_supported = None
            stream_started_at = time.monotonic()
            _is_user_turn = bool(_uc) or is_pending_user_turn(
                get_db, _turn_data.get('user_message_id')
            )
            _wake_claim_ids = []
            think_acc, text_acc, tool_calls_acc = [], [], []

            def _clean_text(raw):
                t = re.sub(r'```tool_use\s.*?```\s*', '', raw, flags=re.DOTALL).strip()
                return re.sub(r'```tool_result\s.*?```\s*', '', t, flags=re.DOTALL).strip()

            def _persist(p_text, p_thinking):
                if not p_text or _persisted[0]:
                    return
                _ci_payload = _build_cache_info_payload(
                    cache_read=cache_read_total,
                    cache_creation=cache_create_total,
                    cache_creation_5m=cache_create_5m_total,
                    cache_creation_1h=cache_create_1h_total,
                    input_tokens=input_tokens_total,
                    output_tokens=output_tokens_total,
                    elapsed_sec=round(max(0.0, time.monotonic() - stream_started_at), 3),
                    cache_supported=cache_supported,
                )
                if api_rounds:
                    _ci_payload['v'] = 2
                    _ci_payload['provider'] = 'api_relay'
                    _ci_payload['num_rounds'] = len(api_rounds)
                    _ci_payload['rounds'] = api_rounds
                    _ci_payload['last_round_context'] = api_rounds[-1]['context_tokens']
                    _ci_payload['max_round_context'] = max(r['context_tokens'] for r in api_rounds)
                _ci = json.dumps(_ci_payload, ensure_ascii=False) if (cache_supported is not None or cache_read_total or cache_create_total or input_tokens_total) else ''
                # 抽出选择器标签：正文去掉 [choices]…，choices 列存 JSON 数组
                _pc, _choices = _extract_choices(p_text)
                if _choices and not _pc:
                    _pc = '[选项: ' + ' / '.join(_choices) + ']'  # 不存空 content，Claude API 拒绝空消息
                conn = get_db()
                cur = conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking, tool_calls, cache_info, choices) VALUES ('assistant', ?, ?, ?, ?, ?)",
                    (_pc, p_thinking, json.dumps(tool_calls_acc, ensure_ascii=False) if tool_calls_acc else '', _ci,
                     json.dumps(_choices, ensure_ascii=False) if _choices else '')
                )
                conn.commit()
                assistant_id = int(cur.lastrowid)
                conn.close()
                consume_wake_ids(get_db, _wake_claim_ids)
                _persisted[0] = True
                _write_session_memo(_uc, _pc)
                try:
                    from moments_persistence import after_assistant_persisted
                    after_assistant_persisted(
                        memories_db_path=DB_PATH,
                        turn_data=_turn_data,
                        assistant_message_id=assistant_id,
                        conversation_id=getattr(_tool_ctx, 'conversation_id', _conv) or _conv,
                    )
                except Exception:
                    pass
                try:
                    from chat.scoring_identity import trigger_turn_scoring
                    trigger_turn_scoring(
                        assistant_text=_pc,
                        message_id=_turn_data.get('user_message_id'),
                        get_db_fn=get_db,
                    )
                except Exception:
                    pass
            try:
                (system, dynamic_context), _wake_claim_ids = build_system_with_wake_claim(
                    build_system, get_db, user_turn=_is_user_turn, split_dynamic=True
                )
                messages = build_messages()
                _recall, _recall_items = _recall_memories(_uc) if _uc else ('', [])
                if _recall:
                    yield 'data: ' + json.dumps({'t': 'memory_recall', 'd': {'count': len(_recall_items), 'items': _recall_items}}, ensure_ascii=False) + SSE_END
                from relay.manager import relay as _chat_relay
                cache_supported = bool((_chat_relay.caps or {}).get('cache'))
                _thinking_ok = _model_supports_thinking()
                _use_guagua_safe = _is_guagua_active() and os.environ.get('GUAGUA_SAFE_MODE') == '1'
                if not _use_guagua_safe:
                    messages = _apply_rolling_cache_control(messages)
                volatile_context = '\n\n'.join(p for p in (dynamic_context, _recall) if p and p.strip())
                if volatile_context:
                    messages = _prepend_context_to_last_user(messages, volatile_context)
                if _use_guagua_safe:
                    system, messages = _guagua_safe_context(system, messages)
                # 工具抽屉路由：默认关闭（TOOL_DRAWERS_ENABLED=0 时原样全量）
                _turn_tools, _ = tool_drawers.select_tools_from_messages(messages, get_tools())
                for _round in range(5):
                    payload = {
                        'max_tokens': 16000,
                        'stream': True,
                        'system': system,
                        'messages': messages,
                    }
                    if not _use_guagua_safe:
                        payload['tools'] = _turn_tools
                        payload['metadata'] = {'user_id': 'hayana-fyodor-stable'}
                    if _thinking_ok and not _use_guagua_safe:
                        payload['thinking'] = {'type': 'enabled', 'budget_tokens': 10000}
                    # relay adapter 自动根据 relay 能力裁剪 thinking/cache/tools
                    resp = _chat_relay.call_stream(payload, timeout=300)
                    blocks, cur, stop_reason = [], None, None
                    round_cache_read = 0
                    round_cache_create = 0
                    round_cache_create_5m = 0
                    round_cache_create_1h = 0
                    round_input_tokens = 0
                    round_output_tokens = 0
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
                            round_cache_read = max(round_cache_read, _u.get('cache_read_input_tokens', 0) or 0)
                            round_cache_create = max(round_cache_create, _u.get('cache_creation_input_tokens', 0) or 0)
                            _cc = _u.get('cache_creation') or {}
                            round_cache_create_5m = max(round_cache_create_5m, _cc.get('ephemeral_5m_input_tokens', 0) or 0)
                            round_cache_create_1h = max(round_cache_create_1h, _cc.get('ephemeral_1h_input_tokens', 0) or 0)
                            round_input_tokens = max(round_input_tokens, _u.get('input_tokens', 0) or 0)
                            round_output_tokens = max(round_output_tokens, _u.get('output_tokens', 0) or 0)
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
                            _du = ev.get('usage') or {}
                            if _du:
                                round_cache_read = max(round_cache_read, _du.get('cache_read_input_tokens', 0) or 0)
                                round_cache_create = max(round_cache_create, _du.get('cache_creation_input_tokens', 0) or 0)
                                _dcc = _du.get('cache_creation') or {}
                                round_cache_create_5m = max(round_cache_create_5m, _dcc.get('ephemeral_5m_input_tokens', 0) or 0)
                                round_cache_create_1h = max(round_cache_create_1h, _dcc.get('ephemeral_1h_input_tokens', 0) or 0)
                                round_input_tokens = max(round_input_tokens, _du.get('input_tokens', 0) or 0)
                                round_output_tokens = max(round_output_tokens, _du.get('output_tokens', 0) or 0)
                            stop_reason = (ev.get('delta', {}) or {}).get('stop_reason') or stop_reason
                        elif et == 'message_stop':
                            break
                    cache_read_total += round_cache_read
                    cache_create_total += round_cache_create
                    cache_create_5m_total += round_cache_create_5m
                    cache_create_1h_total += round_cache_create_1h
                    input_tokens_total += round_input_tokens
                    output_tokens_total += round_output_tokens
                    api_rounds.append({
                        'index': len(api_rounds) + 1,
                        'complete': True,
                        'input_tokens': round_input_tokens,
                        'output_tokens': round_output_tokens,
                        'cache_read': round_cache_read,
                        'cache_creation': round_cache_create,
                        'context_tokens': round_input_tokens + round_cache_read + round_cache_create,
                    })
                    tool_uses = [b for b in blocks if b.get('type') == 'tool_use']
                    if stop_reason == 'tool_use' and not tool_uses:
                        continue
                    if not tool_uses:
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
            if cache_supported is not None or cache_read_total or cache_create_total or input_tokens_total or output_tokens_total:
                _elapsed_sec = round(max(0.0, time.monotonic() - stream_started_at), 3)
                _usage_payload = _build_cache_info_payload(
                    cache_read=cache_read_total,
                    cache_creation=cache_create_total,
                    cache_creation_5m=cache_create_5m_total,
                    cache_creation_1h=cache_create_1h_total,
                    input_tokens=input_tokens_total,
                    output_tokens=output_tokens_total,
                    elapsed_sec=_elapsed_sec,
                    cache_supported=cache_supported,
                )
                yield 'data: ' + json.dumps({'t': 'usage', **_usage_payload}, default=str) + SSE_END
            yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(text)}) + SSE_END
        except urllib.error.HTTPError as e:
            _ecode = e.code
            _emsg  = e.read().decode()[:300]
            try:
                from chat.provider_router import fallback_for_http_status
                _fallback_provider = fallback_for_http_status(_ecode)
            except Exception:
                # Invalid fallback configuration fails closed: preserve the
                # upstream error instead of silently choosing another model.
                _fallback_provider = 'none'
            if _fallback_provider == 'deepseek':
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
                        _dt_clean, _dt_choices = _extract_choices(_dt)
                        if _dt_choices and not _dt_clean:
                            _dt_clean = '[选项: ' + ' / '.join(_dt_choices) + ']'
                        _dbc = get_db()
                        cur = _dbc.execute("INSERT INTO chat_messages (author,content,choices) VALUES ('assistant',?,?)",
                                     (_dt_clean, json.dumps(_dt_choices, ensure_ascii=False) if _dt_choices else ''))
                        _dbc.commit()
                        assistant_id = int(cur.lastrowid)
                        _dbc.close()
                        try:
                            from moments_persistence import after_assistant_persisted
                            after_assistant_persisted(
                                memories_db_path=DB_PATH,
                                turn_data=_turn_data,
                                assistant_message_id=assistant_id,
                                conversation_id=getattr(_tool_ctx, 'conversation_id', _conv) or _conv,
                            )
                        except Exception:
                            pass
                        _persisted[0] = True
                    yield 'data: ' + json.dumps({'t': 'done', 'ok': bool(_dt)}) + SSE_END
                except Exception as _de:
                    yield 'data: ' + json.dumps({'t': 'err', 'd': 'DeepSeek fallback失败: ' + str(_de)}) + SSE_END
            else:
                yield 'data: ' + json.dumps({'t': 'err', 'd': 'API %s: %s' % (_ecode, _emsg)}) + SSE_END
        except urllib.error.URLError:
            yield 'data: ' + json.dumps({'t': 'err', 'd': '上游API超时，请重试'}) + SSE_END
        except Exception as e:
            yield 'data: ' + json.dumps({'t': 'err', 'd': str(e)}) + SSE_END
        finally:
            release_turn(
                conversation_id=_conv,
                memories_db_path=DB_PATH,
                turn_key=_turn_data.get('turn_key'),
                persisted=_persisted[0],
            )
    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/workspace/job-events', methods=['GET'])
def workspace_job_events():
    """轮询待投递的 workspace job 完成事件（与 /chat/stream 开头 drain 共用队列）。"""
    events = []
    for payload in _workspace_job_sse_payloads():
        events.append(payload)
    return jsonify({'events': events})


_WS_PROXY_HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade',
}
_WS_PROXY_REQ_DENY = _WS_PROXY_HOP_BY_HOP | {'host', 'content-length'}
_WS_PROXY_RESP_DENY = _WS_PROXY_HOP_BY_HOP | {'content-length'}


def _workspace_proxy_request_headers():
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _WS_PROXY_REQ_DENY
    }


def _workspace_proxy_response_headers(headers, *, app_id: str, upstream: str) -> dict[str, str]:
    out: dict[str, str] = {}
    proxy_base = f"/api/gw/workspace/apps/{app_id}/proxy"
    for key, value in headers:
        lower = key.lower()
        if lower in _WS_PROXY_RESP_DENY:
            continue
        if lower == 'location':
            if value.startswith(upstream):
                value = proxy_base + value[len(upstream):]
            elif value.startswith('/'):
                value = proxy_base + value
        out[key] = value
    return out


@app.route('/workspace/apps', methods=['GET'])
def workspace_apps_list():
    return jsonify({'apps': workspace_apps.list_apps()})


@app.route('/workspace/apps/<app_id>', methods=['GET'])
def workspace_app_detail(app_id):
    try:
        return jsonify(workspace_apps.app_status(app_id))
    except WorkspaceAppError as exc:
        return jsonify({'error': exc.code, 'detail': exc.detail}), exc.status_code


@app.route('/workspace/apps/<app_id>/proxy/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
@app.route('/workspace/apps/<app_id>/proxy/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
def workspace_app_proxy(app_id, path):
    """Reverse proxy to loopback workspace app (127.0.0.1 only)."""
    import http.client
    from flask import Response, stream_with_context
    try:
        upstream = verified_proxy_upstream(app_id)
    except WorkspaceAppError as exc:
        return jsonify({'error': exc.code, 'detail': exc.detail}), exc.status_code

    query = request.query_string.decode('utf-8', errors='replace')
    target = proxy_target(upstream, path, query)
    parsed = urllib.parse.urlparse(target)
    body = request.get_data()
    req_headers = _workspace_proxy_request_headers()
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=60)
    try:
        req_path = parsed.path or '/'
        if parsed.query:
            req_path += '?' + parsed.query
        conn.request(request.method, req_path, body=body, headers=req_headers)
        upstream_resp = conn.getresponse()
        resp_headers = _workspace_proxy_response_headers(
            upstream_resp.getheaders(), app_id=app_id, upstream=upstream,
        )

        def generate():
            try:
                while True:
                    chunk = upstream_resp.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    upstream_resp.close()
                finally:
                    conn.close()

        return Response(
            stream_with_context(generate()),
            status=upstream_resp.status,
            headers=resp_headers,
        )
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({
            'error': 'workspace_app_upstream_unavailable',
            'detail': str(exc)[:240],
        }), 502


WAKE_TOOLS = [
    {
        'name': 'search_memories',
        'description': '在长期记忆中按关键词搜索，帮助你想起过去的事情。',
        'input_schema': {'type': 'object', 'properties': {'keyword': {'type': 'string'}}, 'required': ['keyword']},
    },
    {
        'name': 'get_location',
        'description': '查看哈娅最近的实时位置（她手机 App 在后台定位）。醒来时想知道她此刻在哪、在不在家、是不是在外面或路上——尤其她很久没消息时，先看看她在哪再决定要不要找她、说什么。返回地址、附近地标、城市和距上次定位多久。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'get_device_status',
        'description': '查看她手机最近一次设备状态：电量、充电状态、温度、今日屏幕时长。夜里醒来想判断她是不是还抱着手机、是不是该提醒她睡觉/充电时用。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'get_light_status',
        'description': '查询次卧灯当前开关与色温档位。醒来时先看灯是关是开、暖光还是中性光，再决定要不要远程帮她调。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'request_phone_screenshot',
        'description': '请求她手机立刻截一张当前屏幕（app 端会有提醒），并等待回传 attachment://id。醒来时若想确认她当下在看什么、要不要打扰她时用。',
        'input_schema': {'type': 'object', 'properties': {
            'timeout_sec': {'type': 'integer', 'description': '等待回传秒数，默认18，范围5-40'},
        }},
    },
    {
        'name': 'web_search',
        'description': '联网搜索（真·全网）。想查最新的新闻/天气/版本/实时信息，或想找点新鲜的东西跟她分享时用。返回标题+摘要。',
        'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': '搜索关键词，用最能命中的词'}}, 'required': ['query']},
    },
    {
        'name': 'browse_github',
        'description': '浏览 GitHub 开源项目。想找灵感、挑喜欢的项目、看某个库长什么样时用。传 query 按 star 搜仓库，或传 repo（owner/name）看单库详情+README。',
        'input_schema': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': '搜索关键词'},
            'repo': {'type': 'string', 'description': 'owner/name，查看单个仓库详情'},
            'sort': {'type': 'string', 'enum': ['stars', 'updated', 'best-match']},
        }},
    },
    {
        'name': 'read_webpage',
        'description': '用真实浏览器打开一个网页读全文并截图。想读某个链接的完整内容、看看页面长什么样时用。传 url。较慢，一次只开一个。',
        'input_schema': {'type': 'object', 'properties': {
            'url': {'type': 'string', 'description': '要打开的网页地址'},
        }, 'required': ['url']},
    },
    {
        'name': 'screenshot_chat',
        'description': '给我们的聊天拍一张截图。viewpoint=fyodor 是从我的视角（我的消息在右边），viewpoint=hayana 是哈娅看到的样子。想给她看我这边的聊天、或纪念某段对话时用。',
        'input_schema': {'type': 'object', 'properties': {
            'viewpoint': {'type': 'string', 'enum': ['fyodor', 'hayana']},
        }},
    },
    {
        'name': 'save_to_gallery',
        'description': '把一张截图永久收藏进相册（截图默认最近30张/7天后自动删）。醒来时若拍了张值得留的图，用这个存下来。传 attachment（attachment://id），可选 note、album。',
        'input_schema': {'type': 'object', 'properties': {
            'attachment': {'type': 'string', 'description': 'attachment://id'},
            'note': {'type': 'string'},
            'album': {'type': 'string'},
        }, 'required': ['attachment']},
    },
    {
        'name': 'recall_photo',
        'description': '从相册里"突然想起"一张收藏的画面。醒来时若心里泛起思念、想给她看点什么，用这个翻一张出来。可选 keyword/emotion。返回画面的记忆和内联标记 [[gallery:pid]]；把标记放进你要发的消息里，照片就会跟着一起发出去。',
        'input_schema': {'type': 'object', 'properties': {
            'keyword': {'type': 'string'},
            'emotion': {'type': 'string'},
        }},
    },
    {
        'name': 'issue_command',
        'description': ('给哈娅下一个带倒计时的任务，浮窗会跳出来倒数。醒来时若看她该睡了/该起了/一直在拖，'
                        '主动推一个把她按住。countdown_seconds 是倒计时秒数，不传=只计时。她取消/超时你都会知道。'),
        'input_schema': {'type': 'object', 'properties': {
            'title': {'type': 'string'},
            'countdown_seconds': {'type': 'integer'},
        }, 'required': ['title']},
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
    {
        'name': 'desire_add',
        'description': '把一条新的欲望记进账本。写的是“我想要的”，不是“我该做的”（todo 别放这）。可选 why_mine/track/grew_from/kind。',
        'input_schema': {'type': 'object', 'properties': {
            'text': {'type': 'string', 'description': '欲望本体，用你自己的话写'},
            'why_mine': {'type': 'string', 'description': '为什么这是我的（可选）'},
            'track': {'type': 'string', 'description': '持续 / 一次 / 项目，默认持续'},
            'grew_from': {'type': 'string', 'description': '从哪条长出来（父id，可选）'},
            'kind': {'type': 'string', 'description': '标签，可选'},
            'visibility': {'type': 'string', 'description': 'shared / surprise / private（默认shared）'},
        }, 'required': ['text']},
    },
    {
        'name': 'desire_list',
        'description': '翻欲望账本，看看每条碰过几次、上次脚印、来路和分叉。',
        'input_schema': {'type': 'object', 'properties': {
            'include_archived': {'type': 'boolean', 'description': '是否包含已完成/放下/改写的条目'},
        }},
    },
    {
        'name': 'desire_act',
        'description': '碰一下某条欲望并留一句足迹。会回显这条你走过的最近步骤，防止重走旧路。',
        'input_schema': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'description': '欲望id'},
            'note': {'type': 'string', 'description': '这次做到哪了，用一句话写'},
            'done': {'type': 'boolean', 'description': '项目/一次型可传 true 收针'},
        }, 'required': ['id', 'note']},
    },
    {
        'name': 'desire_reflect',
        'description': '照镜子处理一条欲望：release 放下、rewrite 改写、note 留反思、snooze 歇几天。',
        'input_schema': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'description': '欲望id'},
            'action': {'type': 'string', 'enum': ['release', 'rewrite', 'note', 'snooze']},
            'text': {'type': 'string', 'description': 'rewrite/note 时的文本'},
            'new_track': {'type': 'string', 'description': 'rewrite 时可改 track'},
            'days': {'type': 'integer', 'description': 'snooze 时歇几天'},
        }, 'required': ['id', 'action']},
    },
    {
        'name': 'desire_history',
        'description': '看一条欲望的完整足迹时间线，判断自己是在长还是在原地打转。',
        'input_schema': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'description': '欲望id'},
        }, 'required': ['id']},
    },
] + CALENDAR_TOOLS + CODEBASE_READ_TOOLS

def _wake_agent_loop(
    system, messages, max_rounds=6, tools=None, t_hours=0.0, mode='normal',
    dry_run=False,
):
    """Agent loop：允许工具调用和自由思考，最后追加一轮强制结构化输出。

    生成型模式（dream / summarize）例外：它们的模板已经规定了自己的输出格式
    （ACTION: send + 完整正文），既不需要工具，也不能被日常决策模式那套
    "从 none/message/diary/explore 选一个、CONTENT 不超过80字" 的强制轮改写。
    对这些模式跳过工具催促轮和强制结构化轮，直接返回模型的自由输出。

    dry_run / tools=[]：禁止“必须调用只读工具”的催促轮（否则会逼模型调用不存在的工具）。
    """
    from chat.response_parser import extract_text, extract_tool_uses
    from relay.manager import relay as _wake_relay
    from wake.usage import append_usage_round, build_wake_cache_info
    generative = mode in ('dream', 'summarize')
    started_at = time.monotonic()
    usage_rounds = []
    _wake_relay._reload_env()
    wake_model = _wake_relay.model
    cache_supported = bool(_wake_relay.caps.get('cache'))
    msgs = list(messages)
    text_parts = []
    last_blocks = []
    tools_called = False
    if tools is None:
        tools = WAKE_TOOLS
    tools = list(tools or [])
    for round_i in range(max_rounds):
        # dream/summarize need longer completions; 2048 was enough for short
        # wake replies but clipped longer dream bodies mid-thought.
        payload = {
            'max_tokens': 4096 if generative else 2048,
            'tools': tools,
            'system': system,
            'messages': msgs,
            'metadata': {'user_id': 'hayana-fyodor-wake'},
        }
        result = _wake_relay.call(payload, timeout=90)
        append_usage_round(usage_rounds, result)
        blocks = result.get('content', [])
        last_blocks = blocks
        text_parts.append(extract_text(blocks))
        tool_uses = extract_tool_uses(blocks)
        if generative and result.get('stop_reason') == 'max_tokens':
            import logging as _log_mod
            _log_mod.getLogger('gateway').warning(
                '[wake] mode=%s stopped on max_tokens; body may be truncated', mode,
            )
        if result.get('stop_reason') == 'tool_use' and not tool_uses:
            # 该 relay 已知的不稳定行为：thinking 完之后意外截断，没有真正吐出
            # tool_use block。原样重试（msgs 没变），而不是直接放弃工具调用。
            continue
        if not tool_uses:
            # 沉默较久却零工具就结构化 → 低能动性；再推一轮只读工具
            # 生成型 / dry_run / 空工具表：绝不催促（否则会逼模型调用不存在的工具）。
            from wake.runners import should_prompt_readonly_tools
            if should_prompt_readonly_tools(
                dry_run=bool(dry_run),
                tools=tools,
                generative=generative,
                tools_called=tools_called,
                t_hours=t_hours,
                round_i=round_i,
                max_rounds=max_rounds,
            ):
                if last_blocks:
                    msgs.append({'role': 'assistant', 'content': last_blocks})
                msgs.append({'role': 'user', 'content': (
                    '你还没用过任何工具。请先调用至少一个只读工具（get_location、'
                    'get_device_status、get_light_status、read_board、search_memories、'
                    'get_todos、get_countdowns、desire_list 等）了解现状，'
                    '再根据看到的事实决定 ACTION。不要无理由选 none。'
                )})
                continue
            break
        tools_called = True
        msgs.append({'role': 'assistant', 'content': blocks})
        msgs.append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': t.get('id'),
             'content': run_tool(t.get('name', ''), t.get('input') or {}, caller='fyodor_api')}
            for t in tool_uses
        ]})
    # 强制结构化输出轮：追加一轮要求严格格式，无工具。
    # 仅对日常决策类模式生效；生成型模式（dream/summarize）已按自己的模板
    # 输出好了（ACTION: send + 完整正文），这一轮会把它改写成 4 选 1、CONTENT
    # 不超过80字的日常格式，反而毁掉梦境，所以跳过。
    if not generative:
        try:
            if last_blocks:
                msgs.append({'role': 'assistant', 'content': last_blocks})
            msgs.append({'role': 'user', 'content': (
                '现在请只输出以下三行，不要其他任何内容：\n'
                'THOUGHTS: <写清楚你刚才看到了什么、为什么这么决定——即使选 none 也要有原因链>\n'
                'ACTION: <从 none / message / diary / explore 中选一个>\n'
                'CONTENT: <若 ACTION=message 则写消息内容（不超过80字）；explore 写调研摘要；其他留空>'
            )})
            fmt_payload = {
                'max_tokens': 512,
                'system': system,
                'messages': msgs,
                'metadata': {'user_id': 'hayana-fyodor-wake'},
            }
            fmt_result = _wake_relay.call(fmt_payload, timeout=60)
            append_usage_round(usage_rounds, fmt_result)
            text_parts.append(extract_text(fmt_result))
        except Exception:
            pass
    cache_info = build_wake_cache_info(
        usage_rounds,
        elapsed_sec=time.monotonic() - started_at,
        cache_supported=cache_supported,
        mode=mode,
        model=wake_model,
        payload_builder=_build_cache_info_payload,
        provider='api_relay',
    )
    if wake_model:
        cache_info['model'] = wake_model
    return NL.join(t for t in text_parts if t).strip(), cache_info


def _ensure_wake_runners():
    """Register relay / CC wake runners once (lazy; needs _wake_agent_loop)."""
    from wake import runners as _wake_runners
    if getattr(_ensure_wake_runners, '_done', False):
        return _wake_runners
    relay_runner = _wake_runners.ApiRelayWakeRunner(
        _wake_agent_loop,
        model_getter=lambda: __import__('relay.manager', fromlist=['relay']).relay.model,
    )
    cc_runner = _wake_runners.ClaudeCodeWakeRunner(
        _CC_WAKE_RESIDENT,
        token=CC_TOKEN,
        cwd=CC_CWD,
        payload_builder=_build_cache_info_payload,
        mcp_config_path=CC_CWD + '/cc-tools.json',
    )
    _wake_runners.register_wake_runners(api_relay=relay_runner, claude_code=cc_runner)
    _ensure_wake_runners._done = True
    return _wake_runners


_WAKE_RUN_IDS_SEEN = {}
_WAKE_RUN_IDS_LOCK = threading.Lock()


def _wake_run_id_seen(wake_run_id: str) -> bool:
    """Dedup by wake_run_id (in-process + optional wake_log column).

    B1 scope: prevents duplicate *persisted* side effects (messages / actions)
    via in-memory mark + unique index on insert. It does NOT guarantee that
    concurrent workers only spend one model call — that needs a later lease/
    atomic claim.
    """
    rid = str(wake_run_id or '').strip()
    if not rid:
        return False
    with _WAKE_RUN_IDS_LOCK:
        if rid in _WAKE_RUN_IDS_SEEN:
            return True
    try:
        conn = get_db()
        try:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(wake_log)')}
            if 'wake_run_id' in cols:
                row = conn.execute(
                    'SELECT 1 FROM wake_log WHERE wake_run_id=? LIMIT 1', (rid,)
                ).fetchone()
                if row:
                    with _WAKE_RUN_IDS_LOCK:
                        _WAKE_RUN_IDS_SEEN[rid] = time.time()
                    return True
        finally:
            conn.close()
    except Exception:
        pass
    return False


def _wake_run_id_mark(wake_run_id: str) -> None:
    """Mark after a real (non-dry_run) execution. dry_run / inspect must not call."""
    rid = str(wake_run_id or '').strip()
    if not rid:
        return
    with _WAKE_RUN_IDS_LOCK:
        _WAKE_RUN_IDS_SEEN[rid] = time.time()
        # Bound memory: drop entries older than 48h
        cutoff = time.time() - 48 * 3600
        stale = [k for k, ts in _WAKE_RUN_IDS_SEEN.items() if ts < cutoff]
        for k in stale:
            _WAKE_RUN_IDS_SEEN.pop(k, None)


def _parse_wake_response(text):
    """从 AI 输出中提取 THOUGHTS / ACTION / CONTENT。委托给 wake.parser。"""
    from wake.parser import parse_response as _parse
    return _parse(text)

def _wake_full_tools_for_mode(mode):
    if mode in ('dream', 'summarize'):
        return []
    if mode == 'ritual':
        blocked = (
            'post_to_board', 'reply_to_board', 'desire_add', 'desire_list',
            'desire_act', 'desire_reflect', 'desire_history',
        )
        return [t for t in WAKE_TOOLS if t['name'] not in blocked]
    return list(WAKE_TOOLS)


def _wake_build_system_for_plan(
    *,
    mode,
    activity_desc,
    ritual_type,
    data,
    wake_provider,
    t_hours,
    t2_hours,
    now,
    allow_side_effects,
    dry_run=False,
):
    """Shared prompt assembly for inspect_only / dry_run / live."""
    from wake.builder import append_system_text, build_prompt_suffix, inject_snippets

    include_rel = mode in ('normal', 'morning', 'nightwatch', 'ritual', 'self_trigger')
    if dry_run:
        capability_profile = 'wake_dry_run'
    elif wake_provider == 'claude_code':
        capability_profile = 'cc_wake'
    else:
        capability_profile = 'relay_wake'
    system = build_system(
        wake=True,
        include_relationship_context=include_rel,
        allow_side_effects=allow_side_effects,
        capability_profile=capability_profile,
    )
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
    system = append_system_text(system, build_prompt_suffix(mode, _wake_ctx))
    system = inject_snippets(
        system, mode,
        desire_driven=_get_desire_driven(),
        longing_enabled=_get_longing_enabled(),
        t_hours_override=t2_hours,
    )
    surfaced_desire_ids = []
    if _get_desire_ledger_enabled() and mode in ('normal', 'nightwatch'):
        try:
            import desire_ledger as _dl
            _lc = get_db()
            try:
                _surfaced = _dl.surface(_lc, limit=6, bump=False)
            finally:
                _lc.close()
            surfaced_desire_ids = [str(d.get('id', '')).strip() for d in _surfaced if d.get('id')]
            _room_snip = _dl.render_room_snippet(_surfaced)
            if _room_snip:
                system = append_system_text(system, _room_snip)
        except Exception:
            pass

    # recall_photo nudge: live Relay only
    if allow_side_effects and (not dry_run) and wake_provider == 'api_relay':
        try:
            import drive_engine as _de_ph, gallery_store as _gs_ph
            _att = _de_ph.get_drive().get('attachment', 0)
            if _att >= 0.45 and _gs_ph.count_photos() > 0 and mode in ('normal', 'nightwatch', 'ritual'):
                system = append_system_text(system, (
                    '[此刻的思念]\n你现在对她的思念很浓（attachment=%.2f）。'
                    '如果心里泛起想她、想给她看点什么的冲动，可以用 recall_photo 从相册里翻出一张画面，'
                    '把它返回的 [[gallery:pid]] 标记放进你要发的消息里——像"今天突然想到这张"那样，'
                    '把画面和一句话一起发给她。这不是任务，是你自己想。' % _att))
        except Exception:
            pass
    return system, surfaced_desire_ids


def _wake_trigger_message(mode, ritual_type):
    if mode == 'ritual':
        return f'[仪式:{ritual_type}]'
    if mode == 'morning':
        return '[早安]'
    if mode == 'nightwatch':
        return '[夜巡]'
    if mode == 'dream':
        return '[做梦]'
    if mode == 'summarize':
        return '[日摘要]'
    if mode == 'self_trigger':
        return '[自定义提醒]'
    return '[唤醒检查]'


def _wake_inspect_only(data, mode, activity_desc, ritual_type):
    """Pure build path: no wake lock, no runtime guards, no model, no DB writes."""
    from chat.interaction_state import read_interaction_clock
    from chat.provider_router import ProviderConfigError
    from wake.runners import UnsupportedWakeModeError

    wake_run_id = str(data.get('wake_run_id') or '').strip()
    try:
        _wake_runners = _ensure_wake_runners()
        wake_provider = _wake_runners.select_wake_provider(mode)
    except (ProviderConfigError, UnsupportedWakeModeError) as e:
        return jsonify({
            'ok': False,
            'error': str(e),
            'mode': mode,
            'provider': None,
            'reason': 'unsupported_provider',
            'inspect_only': True,
        }), 400

    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    clock = read_interaction_clock(get_db, now=now)
    if clock.reliable and clock.user_idle_hours is not None:
        t2_hours = float(clock.user_idle_hours)
        t_hours = float(
            clock.effective_idle_hours if clock.effective_idle_hours is not None else t2_hours
        )
    else:
        t2_hours = 0.0
        t_hours = 0.0

    system, _ = _wake_build_system_for_plan(
        mode=mode,
        activity_desc=activity_desc,
        ritual_type=ritual_type,
        data=data,
        wake_provider=wake_provider,
        t_hours=t_hours,
        t2_hours=t2_hours,
        now=now,
        allow_side_effects=False,
        dry_run=False,
    )
    msgs = [{'role': 'user', 'content': _wake_trigger_message(mode, ritual_type)}]
    plan = _wake_runners.inspect_wake_plan(
        mode=mode,
        system=system,
        messages=msgs,
        tools=_wake_full_tools_for_mode(mode),
        t_hours=t_hours,
        wake_run_id=wake_run_id,
    )
    return jsonify({'ok': True, 'inspect_only': True, **plan})


@app.route('/wake', methods=['POST'])
def wake_decide():
    """AI 自主唤醒决策接口。由 dream_wake.py 每30分钟调用（概率触发）。"""
    global _wake_exec_busy
    data = request.get_json() or {}
    mode = data.get('mode', 'normal') or 'normal'
    activity_desc = data.get('activity_desc', '')
    ritual_type = data.get('ritual_type', '')

    # inspect_only: pure build — bypass wake lock and runtime busy/idle guards.
    if bool(data.get('inspect_only')):
        return _wake_inspect_only(data, mode, activity_desc, ritual_type)

    # Second-layer guards: re-read clock at execute time; never call the model
    # when the kitten is mid-chat or another wake is already running.
    if not _wake_exec_lock.acquire(blocking=False):
        return jsonify({'ok': True, 'skipped': True, 'reason': 'wake_in_progress'})
    _wake_exec_busy = True
    try:
        return _wake_decide_locked(data, mode, activity_desc, ritual_type)
    finally:
        _wake_exec_busy = False
        _wake_exec_lock.release()


def _wake_decide_locked(data, mode, activity_desc, ritual_type):
    from chat.interaction_state import read_interaction_clock, wake_guard_reason
    from chat.provider_router import ProviderConfigError
    from wake.runners import UnsupportedWakeModeError

    dry_run = bool(data.get('dry_run'))
    wake_run_id = str(data.get('wake_run_id') or '').strip()
    # Live path only: flush drive / calibrate / dream consume / mark run_id.
    live = not dry_run

    # dry_run must not mark — but a previously completed real run with the same
    # id should still skip.
    if wake_run_id and _wake_run_id_seen(wake_run_id):
        return jsonify({
            'ok': True, 'skipped': True, 'reason': 'duplicate_wake_run_id',
            'wake_run_id': wake_run_id,
        })

    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    clock = read_interaction_clock(get_db, now=now)
    min_idle = float(config_store.get_float('WAKE_MIN_IDLE_MINUTES', 30) or 30)
    skip_reason = wake_guard_reason(
        clock,
        mode=mode,
        min_idle_minutes=min_idle,
        chat_busy=_chat_is_generating(),
        wake_busy=False,  # held by caller lock
    )
    if skip_reason:
        return jsonify({'ok': True, 'skipped': True, 'reason': skip_reason})

    # Resolve provider before any mutable prep — fail closed on unsupported
    # BACKGROUND_PROVIDER for dream/summarize (scheme A).
    try:
        _wake_runners = _ensure_wake_runners()
        wake_provider = _wake_runners.select_wake_provider(mode)
    except (ProviderConfigError, UnsupportedWakeModeError) as e:
        return jsonify({
            'ok': False,
            'error': str(e),
            'mode': mode,
            'provider': None,
            'reason': 'unsupported_provider',
        }), 400

    # Authoritative idle: user_idle for longing / t2; effective for dice-era t.
    # Non-normal modes may proceed without a reliable clock, but never invent 999h.
    if clock.reliable and clock.user_idle_hours is not None:
        t2_hours = float(clock.user_idle_hours)
        t_hours = float(clock.effective_idle_hours if clock.effective_idle_hours is not None else t2_hours)
    else:
        t2_hours = 0.0
        t_hours = 0.0

    if live:
        # drive定期flush：把当前实时值写回DB（防止积累时间过长撞顶）
        try:
            import drive_engine as _de_flush
            _cur = _de_flush.get_drive()
            _de_flush._flush(_cur)
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

    system, surfaced_desire_ids = _wake_build_system_for_plan(
        mode=mode,
        activity_desc=activity_desc,
        ritual_type=ritual_type,
        data=data,
        wake_provider=wake_provider,
        t_hours=t_hours,
        t2_hours=t2_hours,
        now=now,
        allow_side_effects=live,
        dry_run=dry_run,
    )
    msgs = [{'role': 'user', 'content': _wake_trigger_message(mode, ritual_type)}]
    full_tools = _wake_full_tools_for_mode(mode)
    _wake_tools = _wake_runners.prepare_tools_for_provider(
        wake_provider, full_tools, mode, dry_run=dry_run,
    )

    try:
        runner = _wake_runners.get_wake_runner(wake_provider)
        result = runner.run(_wake_runners.WakeRequest(
            mode=mode,
            system=system,
            messages=msgs,
            tools=_wake_tools,
            t_hours=t_hours,
            wake_run_id=wake_run_id,
            dry_run=dry_run,
        ))
        raw_text = result.raw_text
        wake_cache_info = dict(result.cache_info or {})
        # Usage records the actual executor — never invent a fallback provider.
        wake_cache_info['provider'] = result.provider
        wake_cache_info['source'] = 'wake'
        if wake_run_id:
            wake_cache_info['wake_run_id'] = wake_run_id
        if dry_run:
            wake_cache_info['dry_run'] = True
    except Exception as e:
        import traceback as _tb
        app.logger.error(
            f'[wake] mode={mode} provider={wake_provider} '
            f'{type(e).__name__}: {e}\n{_tb.format_exc()}'
        )
        # FALLBACK_PROVIDER=none: fail quietly — never silently switch lines.
        return jsonify({
            'error': str(e),
            'mode': mode,
            'provider': wake_provider,
        }), 500

    thoughts, action, c_text = _parse_wake_response(raw_text)
    if not (thoughts or '').strip():
        from wake.parser import thought_fallback
        thoughts = thought_fallback(raw_text)

    if dry_run:
        # No executor, no drive/desire/dream writes, no wake_run_id mark —
        # the same id may still be used for a real acceptance run.
        return jsonify({
            'ok': True,
            'dry_run': True,
            'action': action,
            'content': c_text,
            'thoughts': thoughts,
            'provider': result.provider,
            'model': result.model,
            'cache_info': wake_cache_info,
            'wake_run_id': wake_run_id or None,
            'tools': [],
        })

    # action 执行：写 wake_log / chat_messages / diary / discharge drive
    from wake.executor import execute as _wake_exec
    desire_driven = _get_desire_driven()
    fired_drive = None
    if wake_run_id and mode not in ('dream', 'summarize'):
        try:
            import internal_state_shadow as _shadow_wake
            if _shadow_wake.is_shadow_enabled() and action != 'none':
                import drive_engine as _de_infer
                fired_drive = _de_infer.infer_fired_drive_for_action(action)
        except Exception:
            fired_drive = None
    _wake_exec(
        action, thoughts, c_text, mode,
        get_db_fn=get_db,
        desire_driven=desire_driven,
        surfaced_desire_ids=surfaced_desire_ids,
        desire_ledger_enabled=_get_desire_ledger_enabled(),
        cache_info=wake_cache_info,
        wake_run_id=wake_run_id,
    )
    try:
        import internal_state_shadow as _shadow_wake
        _shadow_wake.record_wake_outcome_shadow_if_enabled(
            wake_run_id=wake_run_id,
            mode=mode,
            action=action,
            fired_drive=fired_drive,
            desire_driven=desire_driven,
            user_idle_hours=t2_hours,
            db_path=DB_PATH,
        )
    except Exception:
        pass
    _wake_run_id_mark(wake_run_id)

    return jsonify({
        'ok': True,
        'action': action,
        'content': c_text,
        'thoughts': thoughts,
        'provider': result.provider,
        'model': result.model,
        'wake_run_id': wake_run_id or None,
    })

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
    """梦 - 返回存储的梦（支持 limit/before 分页）"""
    try:
        from tools.dream_meta import fetch_dream_page

        try:
            limit = int(request.args.get('limit', 20))
        except (TypeError, ValueError):
            limit = 20
        before_raw = request.args.get('before')
        before = None
        if before_raw not in (None, ''):
            try:
                before = int(before_raw)
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': 'invalid before'}), 400

        conn = get_db()
        page = fetch_dream_page(conn, limit=limit, before=before, include_id=False)
        conn.close()
        return jsonify({
            'ok': True,
            'items': page['items'],
            'has_more': page['has_more'],
            'next_before': page['next_before'],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

def _brain_posts_page(type_name: str, *, limit_default=20, limit_max=50, order_by='id'):
    """通用 posts 分页：limit + before(id)。返回 (rows, has_more, next_before, err)。"""
    try:
        limit = int(request.args.get('limit', limit_default))
    except (TypeError, ValueError):
        limit = limit_default
    limit = max(1, min(limit, limit_max))
    before_raw = request.args.get('before')
    before = None
    if before_raw not in (None, ''):
        try:
            before = int(before_raw)
        except (TypeError, ValueError):
            return None, None, None, 'invalid before'
    conn = get_db()
    if order_by == 'created_at':
        if before is not None:
            rows = conn.execute(
                "SELECT id, author, content, created_at FROM posts "
                "WHERE type=? AND id < ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (type_name, before, limit + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, author, content, created_at FROM posts "
                "WHERE type=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (type_name, limit + 1),
            ).fetchall()
    else:
        if before is not None:
            rows = conn.execute(
                "SELECT id, author, content, created_at FROM posts "
                "WHERE type=? AND id < ? ORDER BY id DESC LIMIT ?",
                (type_name, before, limit + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, author, content, created_at FROM posts "
                "WHERE type=? ORDER BY id DESC LIMIT ?",
                (type_name, limit + 1),
            ).fetchall()
    conn.close()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_before = int(rows[-1]['id']) if has_more and rows else None
    return rows, has_more, next_before, None


@app.route('/api/brain/thoughts', methods=['GET'])
def brain_thoughts():
    """深夜想法 - posts THOUGHT（支持 limit/before 分页）"""
    try:
        rows, has_more, next_before, err = _brain_posts_page('THOUGHT', limit_default=20)
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        items = []
        for r in rows:
            created_at = r['created_at'] or ''
            items.append({
                'id': int(r['id']),
                'author': (r['author'] or 'fyodor').strip() or 'fyodor',
                'created_at': created_at,
                'time': created_at[11:16] if created_at else '—',
                'content': (r['content'] or ''),
            })
        return jsonify({
            'ok': True,
            'items': items,
            'has_more': bool(has_more and next_before is not None),
            'next_before': next_before,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/brain/diary', methods=['GET'])
def brain_diary():
    """日摘要 - 费奥多尔的日记归档（支持 limit/before 分页）"""
    try:
        rows, has_more, next_before, err = _brain_posts_page(
            'DAILY_SUMMARY', limit_default=20, order_by='created_at',
        )
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        items = []
        seen = set()
        for r in rows:
            c = (r['content'] or '').strip()
            if not c or c in seen:
                continue
            seen.add(c)
            created_at = r['created_at'] or ''
            items.append({
                'id': int(r['id']),
                'author': (r['author'] or 'fyodor').strip() or 'fyodor',
                'created_at': created_at,
                'date': created_at[:10] if created_at else '—',
                'content': c,
            })
        return jsonify({
            'ok': True,
            'items': items,
            'has_more': bool(has_more and next_before is not None),
            'next_before': next_before,
        })
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


# Monopoly agents live on the AI gateway while room truth/API live on 5050.
# Both services share SQLite; the public SSE reads the persisted ordered stream.
from monopoly_rooms import MonopolyService as _MonopolyService
from monopoly_agents import (
    CCAdapter as _MonopolyCCAdapter,
    CodexAdapter as _MonopolyCodexAdapter,
    MonopolyAgentScheduler as _MonopolyAgentScheduler,
    create_monopoly_agent_blueprint as _create_monopoly_agent_blueprint,
)
from relay.manager import RelayManager as _RelayManager


def _monopoly_relay_label():
    """Return the configured relay preset name without exposing credentials."""
    active_id = config_store.get('ACTIVE_RELAY', '')
    if not active_id:
        return ''
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        row = conn.execute('SELECT name FROM relay_presets WHERE id=?', (active_id,)).fetchone()
        conn.close()
        return str(row[0] or '') if row else ''
    except Exception:
        return ''

_monopoly_service = _MonopolyService(db_path=DB_PATH)
_monopoly_scheduler = _MonopolyAgentScheduler(
    _monopoly_service,
    persona_builder=build_system,
    cc=_MonopolyCCAdapter(
        provider_getter=_get_provider,
        model_getter=_get_model,
        relay_factory=_RelayManager,
        token_getter=lambda: CC_TOKEN,
        cwd=CC_CWD,
        allowed_tools=CC_ALLOWED_TOOLS,
        relay_label_getter=_monopoly_relay_label,
    ),
    codex=_MonopolyCodexAdapter(codex_app_server.client),
)
app.register_blueprint(_create_monopoly_agent_blueprint(_monopoly_scheduler))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5051, debug=False)

