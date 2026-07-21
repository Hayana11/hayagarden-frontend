import os, re, json, sqlite3, datetime, base64, uuid, threading, shutil
from flask import Flask, request, jsonify, send_from_directory, abort, Response, stream_with_context
import config_store
import attachment_store
import gallery_store
import command_store
import group_chat_store
import codex_app_server
import context_usage_store
import moments_store
import moments_cover
from moments_auth import OwnerAuthError, require_owner
from context_usage_routes import create_context_usage_blueprint
from moments_routes import create_moments_blueprint
from monopoly_rooms import MonopolyService
from monopoly_routes import create_monopoly_blueprint
from valence_scale import normalize_arousal, normalize_valence

app = Flask(__name__, static_folder='static')
DB_PATH = '/opt/frontend/memories.db'
UPLOAD_DIR = '/opt/frontend/static/uploads'
APP_DIST_DIR = '/opt/frontend/app/dist'

BOARD_TOKEN_FYODOR = os.environ.get('BOARD_TOKEN_FYODOR', '')
CONTEXT_USAGE_REPORT_TOKEN = os.environ.get('CONTEXT_USAGE_REPORT_TOKEN', '')
for line in open('/opt/frontend/.env'):
    k, _, v = line.partition('=')
    k = k.strip(); v = v.strip()
    if k == 'BOARD_TOKEN_FYODOR': BOARD_TOKEN_FYODOR = v
    if k == 'CONTEXT_USAGE_REPORT_TOKEN': CONTEXT_USAGE_REPORT_TOKEN = v
# API_URL/API_KEY/MODEL 不再是这里的冻结常量：谁要发请求，
# 就 new 一个 relay.manager.RelayManager()，永远拿实时值。

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ── 文件收发：chat_messages 新增 file_url/file_name/choices 三列 ──────────
# 延续“哪个字段有值就是哪种气泡”的老规矩（image_url 有值=图片）：
#   file_url 有值=文件卡片，choices 有值(JSON 数组)=选择器按钮组。
# 幂等 migration，跑几次都安全。
FILES_DIR = '/opt/frontend/static/uploads/files'
ALLOWED_FILE_EXT = {'.md', '.txt', '.html', '.htm', '.py', '.js', '.json',
                    '.csv', '.css', '.xml', '.yaml', '.yml', '.log', '.ini', '.sh'}
MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB

def _migrate_chat_columns():
    conn = get_db()
    cols = [r[1] for r in conn.execute('PRAGMA table_info(chat_messages)')]
    ddl = {
        'file_url':  "ALTER TABLE chat_messages ADD COLUMN file_url TEXT DEFAULT ''",
        'file_name': "ALTER TABLE chat_messages ADD COLUMN file_name TEXT DEFAULT ''",
        'choices':   "ALTER TABLE chat_messages ADD COLUMN choices TEXT DEFAULT ''",
    }
    for col, stmt in ddl.items():
        if col not in cols:
            conn.execute(stmt)
    conn.commit()
    conn.close()

_migrate_chat_columns()
group_chat_store.ensure_schema(DB_PATH)
context_usage_store.ensure_schema(DB_PATH)
moments_store.ensure_schema(DB_PATH, gallery_store.DB_PATH)
app.register_blueprint(create_context_usage_blueprint(
    db_path=DB_PATH,
    report_token_getter=lambda: CONTEXT_USAGE_REPORT_TOKEN,
))
app.register_blueprint(create_moments_blueprint(
    memories_db_path=DB_PATH,
    gallery_db_path=gallery_store.DB_PATH,
))
app.register_blueprint(create_monopoly_blueprint(MonopolyService(db_path=DB_PATH)))



@app.route('/api/client-error', methods=['POST'])
def client_error_log():
    """Tiny WebView error sink for pages where DevTools are unavailable."""
    try:
        data = request.get_json(silent=True) or {}
        row = {
            'ts': datetime.datetime.now().isoformat(timespec='seconds'),
            'page': str(data.get('page', ''))[:200],
            'msg': str(data.get('msg', ''))[:1000],
            'src': str(data.get('src', ''))[:300],
            'line': data.get('line', 0),
            'col': data.get('col', 0),
            'ua': str(request.headers.get('User-Agent', ''))[:300],
        }
        with open('/opt/frontend/client_errors.log', 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception:
        pass
    return jsonify({'ok': True})


# ── Artifact（费佳生成的 HTML/Markdown/Word 产物）────────────────
@app.route('/api/artifacts/<int:aid>', methods=['GET'])
def artifact_meta(aid):
    import artifact_store
    meta = artifact_store.get(aid)
    if not meta:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'id': meta['id'], 'type': meta['type'], 'title': meta['title'],
        'size': meta['size'], 'created_at': meta['created_at'],
    })

@app.route('/api/artifacts/<int:aid>/preview', methods=['GET'])
def artifact_preview(aid):
    import artifact_store
    from flask import Response
    meta, content = artifact_store.read_content(aid)
    if not meta or content is None:
        return jsonify({'error': 'not found'}), 404
    if meta['type'] == 'html':
        return Response(content, mimetype='text/html')
    if meta['type'] == 'markdown':
        import markdown as _md
        html_body = _md.markdown(content.decode('utf-8'), extensions=['fenced_code', 'tables'])
        page = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + meta['title'].replace('<', '').replace('>', '') + '</title>'
            '<style>body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:720px;'
            'margin:40px auto;padding:0 20px;line-height:1.7;color:#2a2020}'
            'h1,h2,h3{color:#5a4a6a}pre{background:#f5f0e8;padding:12px;border-radius:8px;overflow-x:auto}'
            'code{background:#f5f0e8;padding:1px 5px;border-radius:4px}'
            'table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px 10px}</style>'
            '</head><body>' + html_body + '</body></html>'
        )
        return Response(page, mimetype='text/html')
    return jsonify({'error': 'docx 不支持在线预览，直接下载查看',
                     'download_url': '/api/artifacts/%d/download' % aid}), 400

@app.route('/api/artifacts/<int:aid>/download', methods=['GET'])
def artifact_download(aid):
    import artifact_store
    import urllib.parse as _up
    from flask import Response
    meta, content = artifact_store.read_content(aid)
    if not meta or content is None:
        return jsonify({'error': 'not found'}), 404
    ext = artifact_store.EXT_BY_TYPE[meta['type']]
    safe_title = re.sub(r'[^\w\u4e00-\u9fff-]', '_', meta['title'])[:60] or 'artifact'
    fname = safe_title + '.' + ext
    mime = artifact_store.MIME_BY_TYPE[meta['type']]
    resp = Response(content, mimetype=mime)
    resp.headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + _up.quote(fname)
    return resp


@app.route('/')
def index():
    from flask import redirect
    return redirect('/dash', code=302)

@app.route('/dash')
def dash():
    # Prefer the new React build when deployed; fallback to legacy static dash.
    if os.path.exists(os.path.join(APP_DIST_DIR, 'index.html')):
        resp = send_from_directory(APP_DIST_DIR, 'index.html')
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        return resp
    return send_from_directory('/opt/frontend/static', 'dash.html')

@app.route('/dash/<path:subpath>')
def dash_subpath(subpath):
    if not os.path.exists(os.path.join(APP_DIST_DIR, 'index.html')):
        return send_from_directory('/opt/frontend/static', 'dash.html')
    asset_path = os.path.join(APP_DIST_DIR, subpath)
    if os.path.isfile(asset_path):
        return send_from_directory(APP_DIST_DIR, subpath)
    # React Router fallback
    resp = send_from_directory(APP_DIST_DIR, 'index.html')
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resp

@app.route('/chat')
def chat():
    return send_from_directory('/opt/frontend/static', 'chat.html')

@app.route('/calendar')
def calendar():
    return send_from_directory('/opt/frontend/static', 'calendar.html')

@app.route('/pocket-settings.html')
def pocket_settings_page():
    return send_from_directory('/opt/frontend/static', 'pocket-settings.html')

@app.route('/letters')
def letters():
    return send_from_directory('/opt/frontend/static', 'letters.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('/opt/frontend/static', 'manifest.json')

@app.route('/sw.js')
def sw():
    resp = send_from_directory('/opt/frontend/static', 'sw.js')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/icon-<size>.png')
def icon(size):
    return send_from_directory('/opt/frontend/static', f'icon-{size}.png')

@app.route('/api/posts', methods=['GET'])
def get_posts():
    t        = request.args.get('type','')
    tags     = request.args.get('tags','')
    search   = request.args.get('search','')
    resolved = request.args.get('resolved','')
    layer    = request.args.get('layer','')
    limit    = min(int(request.args.get('limit','200')), 1000)
    where, params = [], []
    if t:
        where.append('type=?'); params.append(t)
    if layer:
        where.append('layer=?'); params.append(layer)
    if tags:
        where.append('tags LIKE ?'); params.append('%'+tags+'%')
    if search:
        where.append('content LIKE ?'); params.append('%'+search+'%')
    if resolved != '':
        where.append('resolved=?'); params.append(int(resolved))
    clause = ('WHERE '+' AND '.join(where)) if where else ''
    conn = get_db()
    rows = conn.execute(
        f'SELECT * FROM posts {clause} ORDER BY id DESC LIMIT ?',
        params+[limit]
    ).fetchall()
    conn.close()
    return jsonify({"posts":[dict(r) for r in rows]})

@app.route('/api/posts', methods=['POST'])
def create_post():
    data = request.get_json()
    content = data.get('content','').strip()
    if not content:
        return jsonify({"error":"empty"}),400
    # 防止把未输出完的<thinking>原始块当成正文存进来（输出被截断时常见）
    if '<thinking>' in content and '</thinking>' not in content:
        content = content.split('<thinking>')[0].strip()
        if not content:
            return jsonify({"error":"content looks like an unterminated <thinking> block, nothing to save"}), 400
    tags = data.get('tags','')
    from tools import memory_tool
    new_id = memory_tool.save_memory(
        content, type=data.get('type','MEMORY'), author=data.get('author','user'),
        layer=data.get('layer','recent'), tags=tags)
    return jsonify({"ok":True,"id":new_id})

@app.route('/api/posts/<int:pid>', methods=['DELETE'])
def delete_post(pid):
    deleted = moments_store.delete_post(pid, memories_db_path=DB_PATH)
    if not deleted:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True})

@app.route('/api/letters', methods=['GET'])
def get_letters():
    conn = get_db()
    rows = conn.execute("SELECT * FROM letters ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify({"letters":[dict(r) for r in rows]})

@app.route('/api/letters', methods=['POST'])
def add_letter():
    data = request.get_json()
    conn = get_db()
    conn.execute("INSERT INTO letters (from_who,to_who,content) VALUES (?,?,?)",
        (data.get('from_who','user'), data.get('to_who','fyodor'), data.get('content','')))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route('/api/countdowns', methods=['GET'])
def get_countdowns():
    conn = get_db()
    rows = conn.execute("SELECT * FROM countdowns ORDER BY id").fetchall()
    conn.close()
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    result = []
    for r in rows:
        d = dict(r)
        target = datetime.datetime.strptime(r['target_date'],'%Y-%m-%d')
        d['days'] = abs((target-now).days)
        result.append(d)
    return jsonify({"countdowns":result})

@app.route('/api/countdowns', methods=['POST'])
def add_countdown():
    data = request.get_json()
    conn = get_db()
    conn.execute("INSERT INTO countdowns (title,target_date,emoji,type) VALUES (?,?,?,?)",
        (data['title'], data['target_date'], data.get('emoji','📅'), data.get('type','countdown')))
    conn.commit()
    conn.close()
    return jsonify({"ok":True})

@app.route('/api/posts/summary', methods=['GET'])
def posts_summary():
    conn = get_db()
    post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
    layer_expr = "layer" if "layer" in post_cols else "'recent'"
    author_expr = "author" if "author" in post_cols else "''"
    rows = conn.execute(
        f"SELECT id, content, {author_expr} as author, {layer_expr} as layer, created_at FROM posts ORDER BY id DESC LIMIT 600"
    ).fetchall()
    conn.close()

    core_items, long_items, recent_items = [], [], []
    for r in rows:
        layer = (r['layer'] or 'recent').strip()
        item = {
            'text': (r['content'] or '').strip()[:120],
            'date': (r['created_at'] or '').split(' ')[0].replace('-', '/'),
            'who': 'haya' if (r['author'] or '').strip() == 'haya' else 'fy',
        }
        if layer == 'core':
            core_items.append(item)
        elif layer in ('long', 'long-term'):
            long_items.append(item)
        else:
            recent_items.append(item)

    fy_count = sum(1 for r in rows if (r['author'] or '').strip() != 'haya')
    haya_count = max(0, len(rows) - fy_count)
    total = max(1, fy_count + haya_count)
    gradient = round(haya_count / total, 3)
    return jsonify({
        'core': len(core_items),
        'long': len(long_items),
        'recent': len(recent_items),
        'gradient': gradient,
        'sections': [
            {'key': 'core', 'title': '核心记忆', 'count': len(core_items), 'items': core_items[:3]},
            {'key': 'long', 'title': '长期记忆', 'count': len(long_items), 'items': long_items[:3]},
            {'key': 'recent', 'title': '近期记忆', 'count': len(recent_items), 'items': recent_items[:3]},
        ],
    })

@app.route('/api/posts/calendar', methods=['GET'])
def posts_calendar():
    month = (request.args.get('month') or '').strip()
    if not re.match(r'^\d{4}-\d{2}$', month):
        month = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m')
    conn = get_db()
    rows = conn.execute(
        "SELECT substr(created_at,1,10) as day FROM posts WHERE created_at LIKE ?",
        (month + '%',)
    ).fetchall()
    conn.close()
    by_day = {}
    for r in rows:
        day = (r['day'] or '')[-2:]
        if day.isdigit():
            by_day[int(day)] = by_day.get(int(day), 0) + 1
    year, mon = int(month[:4]), int(month[5:7])
    dim = (datetime.date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - datetime.timedelta(days=1)).day
    days = [{'day': d, 'hasMemory': by_day.get(d, 0) > 0} for d in range(1, dim + 1)]
    return jsonify({'count': sum(by_day.values()), 'days': days})

@app.route('/api/memories/library', methods=['GET'])
def memories_library():
    from tools import memory_library
    conn = get_db()
    try:
        return jsonify(memory_library.build_memory_library(conn))
    finally:
        conn.close()

@app.route('/api/posts/calendar/day', methods=['GET'])
def posts_calendar_day():
    date_str = (request.args.get('date') or '').strip()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'entries': []})
    conn = get_db()
    rows = conn.execute(
        "SELECT content, type, created_at FROM posts WHERE substr(created_at,1,10)=? ORDER BY id DESC LIMIT 12",
        (date_str,)
    ).fetchall()
    conn.close()
    entries = []
    for r in rows:
        entries.append({
            'cat': (r['type'] or 'Memory').title(),
            'title': (r['content'] or '').strip()[:140] or '未命名记录',
            'date': date_str,
        })
    return jsonify({'entries': entries})

@app.route('/api/messages/heatmap', methods=['GET'])
def messages_heatmap():
    month = (request.args.get('month') or '').strip()
    now_cn = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    if not re.match(r'^\d{4}-\d{2}$', month):
        month = now_cn.strftime('%Y-%m')
    conn = get_db()
    rows = conn.execute(
        "SELECT substr(created_at,1,10) as day, count(*) as cnt FROM chat_messages WHERE created_at LIKE ? GROUP BY substr(created_at,1,10)",
        (month + '%',)
    ).fetchall()
    conn.close()
    days = []
    today_count = 0
    for r in rows:
        dstr = (r['day'] or '')
        if len(dstr) >= 10 and dstr[-2:].isdigit():
            day = int(dstr[-2:])
            cnt = int(r['cnt'] or 0)
            days.append({'day': day, 'count': cnt})
            if dstr == now_cn.strftime('%Y-%m-%d'):
                today_count = cnt
    days.sort(key=lambda x: x['day'])
    # Placeholder streak: count continuous non-zero days from latest backward.
    streak = 0
    if days:
        day_map = {d['day']: d['count'] for d in days}
        cur_day = max(day_map)
        while cur_day > 0 and day_map.get(cur_day, 0) > 0:
            streak += 1
            cur_day -= 1
    return jsonify({'days': days, 'todayCount': today_count, 'streakDays': streak})

@app.route('/api/usage/summary', methods=['GET'])
def usage_summary():
    now_cn = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today = now_cn.strftime('%Y-%m-%d')
    conn = get_db()
    row = conn.execute(
        "SELECT count(*) as c FROM chat_messages WHERE created_at >= ? AND created_at < ?",
        (today + ' 00:00:00', today + ' 23:59:59')
    ).fetchone()
    week_rows = conn.execute(
        "SELECT substr(created_at,1,10) as day, count(*) as c FROM chat_messages "
        "WHERE created_at >= datetime('now','+8 hours','-6 day') GROUP BY substr(created_at,1,10) ORDER BY day"
    ).fetchall()
    conn.close()
    msg_today = int((row['c'] if row else 0) or 0)
    token_today = msg_today * 180  # placeholder estimate until real token aggregation is available
    bars = []
    for r in week_rows:
        c = int(r['c'] or 0)
        bars.append({
            'date': r['day'],
            'fy': int(c * 0.55 * 2),
            'haya': int(c * 0.45 * 2),
        })
    # Ensure 7 bars (placeholder padding) so chart layout stays stable.
    by_day = {b['date']: b for b in bars}
    padded = []
    for i in range(6, -1, -1):
        d = (now_cn - datetime.timedelta(days=i)).strftime('%Y-%m-%d')
        padded.append(by_day.get(d, {'date': d, 'fy': 0, 'haya': 0}))
    return jsonify({
        'win5Pct': min(100, max(0, msg_today * 2)),
        'win5ResetAt': (now_cn + datetime.timedelta(hours=5)).isoformat(),
        'win7Pct': min(100, max(0, int(sum((b['fy'] + b['haya']) for b in padded) / 10))),
        'win7ResetAt': (now_cn + datetime.timedelta(days=7)).isoformat(),
        'msgToday': msg_today,
        'tokenToday': token_today,
        'bars': padded,
    })


def _month_message_count():
    now_cn = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    month_prefix = now_cn.strftime('%Y-%m')
    conn = get_db()
    row = conn.execute(
        "SELECT count(*) as c FROM chat_messages WHERE substr(created_at,1,7)=?",
        (month_prefix,),
    ).fetchone()
    conn.close()
    return int((row['c'] if row else 0) or 0)


def _usage_daily_from_messages(days: int):
    now_cn = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    start_day = (now_cn - datetime.timedelta(days=days - 1)).strftime('%Y-%m-%d')
    conn = get_db()
    rows = conn.execute(
        "SELECT substr(created_at,1,10) as day, count(*) as cnt FROM chat_messages "
        "WHERE substr(created_at,1,10) >= ? GROUP BY substr(created_at,1,10)",
        (start_day,),
    ).fetchall()
    conn.close()
    counts = {r['day']: int(r['cnt'] or 0) for r in rows if r['day']}
    day_rows = []
    for offset in range(days):
        day = (now_cn - datetime.timedelta(days=days - offset - 1)).strftime('%Y-%m-%d')
        count = counts.get(day, 0)
        day_rows.append({'date': day, 'count': count, 'cost': None})
    total_count = sum(item['count'] for item in day_rows)
    return {
        'ok': True,
        'mode': 'requests',
        'days': day_rows,
        'relays': [],
        'total_cost': None,
        'total_count': total_count,
        'month_messages': _month_message_count(),
    }


@app.route('/api/usage/cc-observability', methods=['GET'])
def usage_cc_observability():
    """只读 Claude Code Usage 行李透视日报（阶段 1A）。不改 daily-cost 语义。"""
    from tools.cc_usage_observability import build_report_from_db, clamp_report_days
    days = clamp_report_days(request.args.get('days', 14, type=int))
    try:
        return jsonify(build_report_from_db(DB_PATH, days=days))
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/usage/daily-cost', methods=['GET'])
def usage_daily_cost():
    """Daily usage calendar: relay console costs when credentials exist, else chat request counts."""
    days = min(max(request.args.get('days', 30, type=int), 1), 90)
    _init_relay_account_credentials_table()
    conn = get_db()
    rows = conn.execute('''
        SELECT r.id, r.name, r.url, c.user_id, c.credential_kind, c.secret_ciphertext
        FROM relay_presets r
        INNER JOIN relay_account_credentials c ON c.preset_id = r.id
        WHERE c.secret_ciphertext IS NOT NULL AND c.secret_ciphertext != ''
    ''').fetchall()
    conn.close()
    if not rows:
        return jsonify(_usage_daily_from_messages(days))

    from relay.channel_intelligence import origin_from_url, query_channel_daily_costs
    from relay.credential_vault import decrypt_secret

    now_cn = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today_str = now_cn.strftime('%Y-%m-%d')
    merged = {}
    relay_totals = []
    seen_accounts = set()
    for row in rows:
        try:
            account_key = str(row['user_id'] or '').strip()
            if not account_key or account_key in seen_accounts:
                continue
            secret = decrypt_secret(row['secret_ciphertext'])
            result = query_channel_daily_costs(
                {'id': row['id'], 'name': row['name'], 'base_url': row['url']},
                credential_kind=row['credential_kind'],
                credential_secret=secret,
                user_id=row['user_id'],
                days=days,
            )
            if not result.get('supported'):
                continue
            seen_accounts.add(account_key)
            relay_total = 0.0
            today_cost = 0.0
            daily_costs = {}
            for item in result.get('days') or []:
                bucket = merged.setdefault(item['date'], {'cost': 0.0, 'count': 0})
                bucket['cost'] += float(item.get('cost') or 0)
                bucket['count'] += int(item.get('count') or 0)
                relay_total += float(item.get('cost') or 0)
                daily_costs[item['date']] = round(float(item.get('cost') or 0), 2)
                if item.get('date') == today_str:
                    today_cost = float(item.get('cost') or 0)
            relay_totals.append({
                'id': row['id'],
                'name': row['name'],
                'total_cost': round(relay_total, 2),
                'today_cost': round(today_cost, 2),
                'daily_costs': daily_costs,
            })
        except Exception:
            continue

    if not merged:
        return jsonify(_usage_daily_from_messages(days))

    now_cn = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    day_rows = []
    total_cost = 0.0
    total_count = 0
    for offset in range(days):
        day = (now_cn - datetime.timedelta(days=days - offset - 1)).strftime('%Y-%m-%d')
        bucket = merged.get(day, {'cost': 0.0, 'count': 0})
        cost = round(float(bucket['cost']), 2)
        count = int(bucket['count'])
        total_cost += cost
        total_count += count
        day_rows.append({'date': day, 'cost': cost, 'count': count})

    return jsonify({
        'ok': True,
        'mode': 'cost',
        'days': day_rows,
        'relays': relay_totals,
        'total_cost': round(total_cost, 2),
        'total_count': total_count,
        'month_messages': _month_message_count(),
    })


@app.route('/api/usage/stream', methods=['GET'])
def usage_stream():
    def generate():
        payload = usage_summary().get_json() or {}
        yield 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'
    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/upload', methods=['POST'])
def upload_image():
    if 'file' not in request.files:
        return jsonify({"error":"no file"}),400
    f = request.files['file']
    ext = os.path.splitext(f.filename)[1].lower() or '.jpg'
    fname = f"img_{uuid.uuid4().hex[:8]}{ext}"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    f.save(os.path.join(UPLOAD_DIR, fname))
    return jsonify({"ok":True,"url":f"/static/uploads/{fname}"})

@app.route('/api/chat/upload_file', methods=['POST'])
def upload_file():
    """用户发文本类文件（与图片上传分开，图片走压缩管线、文件不需要）。
    三道防御：扩展名白名单 + 2MB 上限 + 文件名安全化后加随机前缀存储。"""
    if 'file' not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files['file']
    orig = os.path.basename(f.filename or 'file')
    ext = os.path.splitext(orig)[1].lower()
    if ext not in ALLOWED_FILE_EXT:
        return jsonify({"error": "不支持的文件类型（只收文本类）"}), 400
    data = f.read()
    if len(data) > MAX_FILE_BYTES:
        return jsonify({"error": "文件超过 2MB"}), 400
    safe = re.sub(r'[^\w\u4e00-\u9fff.\-]', '_', orig)
    fname = f"{uuid.uuid4().hex[:8]}_{safe}"
    os.makedirs(FILES_DIR, exist_ok=True)
    with open(os.path.join(FILES_DIR, fname), 'wb') as out:
        out.write(data)
    return jsonify({"ok": True, "file_url": f"/static/uploads/files/{fname}", "file_name": safe})

@app.route('/files')
def files_page():
    return send_from_directory('/opt/frontend/static', 'files.html')

@app.route('/api/shop/import_state', methods=['POST'])
def shop_import_state():
    """导入购物登录态（storageState/cookie）到 private/shop_state/<site>.json。

    登录态需要真实浏览器扫码/短信验证才能拿到（走在另一台有显示器的机器上），
    拿到后需要推过来给 headless 的 VPS 用。用一次性 nonce 鉴权（nonce 写在
    /tmp/shop_import_nonce，用完即删），避免把长期密钥挠在代码里。登录态敏感，
    只写固定目录、site 名安全化后作文件名。"""
    import os as _os
    NONCE_FILE = '/tmp/shop_import_nonce'
    data = request.get_json(silent=True) or {}
    nonce = (data.get('nonce') or '').strip()
    try:
        real = open(NONCE_FILE).read().strip()
    except Exception:
        return jsonify({'error': 'no active import window'}), 403
    if not nonce or nonce != real:
        return jsonify({'error': 'bad nonce'}), 403
    site = re.sub(r'[^a-z0-9_-]', '', (data.get('site') or '').lower()) or 'taobao'
    state = data.get('state')
    if not isinstance(state, dict) or 'cookies' not in state:
        return jsonify({'error': 'state must be an object with cookies'}), 400
    d = '/opt/frontend/private/shop_state'
    _os.makedirs(d, exist_ok=True)
    with open(_os.path.join(d, site + '.json'), 'w') as f:
        json.dump(state, f, ensure_ascii=False)
    try:
        _os.remove(NONCE_FILE)  # 一次性，用完作废
    except OSError:
        pass
    return jsonify({'ok': True, 'site': site, 'cookies': len(state.get('cookies') or [])})

@app.route('/api/files/list', methods=['GET'])
def files_list():
    """文档库：查消息表而非扫目录——天然带作者/时间/会话归属，且无孤儿索引。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, author, file_name, file_url, created_at, session_id FROM chat_messages "
        "WHERE file_url != '' ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return jsonify({'files': [dict(r) for r in rows]})

@app.route('/api/files/delete', methods=['POST'])
def files_delete():
    """删物理文件 + 清空消息的 file 字段（消息本体保留，只是不再是文件卡片）。
    删物理文件防路径穿越：realpath 后再校验前缀，即使 DB 被塞恶意 url 也删不出 FILES_DIR。"""
    data = request.get_json() or {}
    ids = data.get('ids') or []
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'ids required'}), 400
    ids = ids[:50]
    conn = get_db()
    deleted = 0
    for mid in ids:
        try:
            mid = int(mid)
        except (ValueError, TypeError):
            continue
        row = conn.execute('SELECT file_url FROM chat_messages WHERE id=?', (mid,)).fetchone()
        if not row or not row['file_url']:
            continue
        url = row['file_url']
        if url.startswith('/static/uploads/files/'):
            p = os.path.realpath('/opt/frontend' + url)
            if p.startswith(os.path.realpath(FILES_DIR)) and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        conn.execute("UPDATE chat_messages SET file_url='', file_name='' WHERE id=?", (mid,))
        deleted += 1
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted': deleted})

@app.route('/api/chat/messages', methods=['GET'])
def get_chat_messages():
    limit = request.args.get('limit', 50, type=int)
    limit = min(max(limit, 1), 200)
    around = request.args.get('around', None, type=int)
    before = request.args.get('before', None, type=int)
    after = request.args.get('after', None, type=int)
    conn = get_db()
    has_more_before = False
    has_more_after = False
    if around:
        half = limit // 2
        start_id = max(1, around - half)
        rows = list(conn.execute(
            "SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC LIMIT ?",
            (start_id, limit)
        ).fetchall())
        if rows:
            first_id = rows[0]['id']
            last_id = rows[-1]['id']
            has_more_before = conn.execute(
                "SELECT 1 FROM chat_messages WHERE id < ? LIMIT 1",
                (first_id,)
            ).fetchone() is not None
            has_more_after = conn.execute(
                "SELECT 1 FROM chat_messages WHERE id > ? LIMIT 1",
                (last_id,)
            ).fetchone() is not None
    elif before:
        rows_desc = list(conn.execute(
            "SELECT * FROM chat_messages WHERE id < ? ORDER BY id DESC LIMIT ?",
            (before, limit + 1)
        ).fetchall())
        has_more_before = len(rows_desc) > limit
        rows_desc = rows_desc[:limit]
        rows = list(reversed(rows_desc))
        has_more_after = True
    elif after:
        rows = list(conn.execute(
            "SELECT * FROM chat_messages WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after, limit + 1)
        ).fetchall())
        has_more_after = len(rows) > limit
        rows = rows[:limit]
        if rows:
            has_more_before = conn.execute(
                "SELECT 1 FROM chat_messages WHERE id < ? LIMIT 1",
                (rows[0]['id'],)
            ).fetchone() is not None
    else:
        rows = list(conn.execute(
            "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?",
            (limit + 1,)
        ).fetchall())
        has_more_before = len(rows) > limit
        rows = rows[:limit]
        rows = list(reversed(rows))
    conn.close()
    return jsonify({
        "messages": [dict(r) for r in rows],
        "has_more_before": bool(has_more_before),
        "has_more_after": bool(has_more_after),
    })


def _group_chat_secret_present(name):
    """Check whether a secret exists without ever returning its value."""
    if (os.environ.get(name) or '').strip():
        return True
    try:
        with open('/opt/frontend/.env', encoding='utf-8') as env_file:
            for raw_line in env_file:
                key, sep, value = raw_line.partition('=')
                if sep and key.strip() == name and value.strip():
                    return True
    except OSError:
        pass
    return False


@app.route('/api/group-chat/status', methods=['GET'])
def group_chat_status():
    claude_ready = bool(shutil.which('claude') and _group_chat_secret_present(
        'CLAUDE_CODE_OAUTH_TOKEN'
    ))
    codex_status = codex_app_server.runtime_status()
    return jsonify({
        'agents': {
            'claude': {
                'ready': claude_ready,
                'color': 'sage',
                'detail': '可以回复' if claude_ready else '暖色线路尚未就绪',
            },
            'codex': {
                'ready': codex_status['ready'],
                'installed': codex_status['installed'],
                'authenticated': codex_status['authenticated'],
                'color': 'blue',
                'detail': codex_status['detail'],
            },
        }
    })


@app.route('/api/group-chat/messages', methods=['GET'])
def group_chat_messages():
    try:
        room = request.args.get('room', 'group')
        limit = request.args.get('limit', 120, type=int)
        before = request.args.get('before', None, type=int)
        messages, has_more = group_chat_store.list_messages(
            room, limit=limit, before=before, db_path=DB_PATH
        )
        return jsonify({'messages': messages, 'has_more_before': has_more})
    except (TypeError, ValueError) as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/group-chat/send', methods=['POST'])
def group_chat_send():
    import json as _json
    data = request.get_json(silent=True) or {}
    content = (data.get('content') or '').strip()
    file_url = (data.get('file_url') or '').strip()
    file_name = (data.get('file_name') or '').strip()
    if not content and not file_url:
        return jsonify({'error': 'content required'}), 400
    if file_url and not content:
        content = '[文件:%s]' % (file_name or '附件')
    if len(content) > 12000:
        return jsonify({'error': 'message too long'}), 400
    meta = _json.dumps({'file_url': file_url, 'file_name': file_name}, ensure_ascii=False) if file_url else ''
    try:
        message = group_chat_store.add_message(
            data.get('room', 'group'), 'user', content, meta=meta, db_path=DB_PATH
        )
        return jsonify({'ok': True, 'message': message})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/group-chat/clear', methods=['POST'])
def group_chat_clear():
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify({'error': 'confirm required'}), 400
    try:
        deleted = group_chat_store.clear_room(
            data.get('room', 'group'), db_path=DB_PATH
        )
        return jsonify({'ok': True, 'deleted': deleted})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

@app.route('/api/chat/send', methods=['POST'])
def send_chat():
    ct = request.content_type or ''
    file_url = file_name = ''
    if 'application/json' in ct:
        data = request.get_json()
        author    = data.get('author', 'user')
        content   = data.get('content', '').strip()
        image_url = data.get('image_url', '')
        file_url  = (data.get('file_url') or '').strip()
        file_name = (data.get('file_name') or '').strip()
    else:
        author  = request.form.get('author', 'user')
        content = request.form.get('content', '').strip()
        image_url = ''
        file_url  = (request.form.get('file_url') or '').strip()
        file_name = (request.form.get('file_name') or '').strip()
        if 'image' in request.files:
            f = request.files['image']
            ext   = os.path.splitext(f.filename)[1].lower() or '.jpg'
            fname = f"img_{uuid.uuid4().hex[:8]}{ext}"
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            f.save(os.path.join(UPLOAD_DIR, fname))
            image_url = f"/static/uploads/{fname}"
    if not content and not image_url and not file_url:
        return jsonify({"error":"empty"}), 400
    # 文件消息的 content 给个可读标记，历史回放给模型时能看懂自己发过什么
    if file_url and not content:
        content = '[文件:%s]' % (file_name or '附件')
    conn = get_db()
    previous_user_at = None
    created_at = None
    message_id = None
    _user_events_requested = all(
        str(os.environ.get(name, '0')).strip() == '1'
        for name in (
            'INTERNAL_STATE_V3_SHADOW_ENABLED',
            'INTERNAL_STATE_V3_SCORE_PROOF_ENABLED',
            'INTERNAL_STATE_V3_USER_EVENTS_ENABLED',
        )
    )
    try:
        if _user_events_requested and author not in ('fyodor', 'assistant', 'claude'):
            from chat.interaction_state import USER_AUTHOR_SQL
            prev = conn.execute(
                f"SELECT created_at FROM chat_messages WHERE {USER_AUTHOR_SQL} "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if prev is not None:
                previous_user_at = prev['created_at'] if hasattr(prev, 'keys') else prev[0]
                previous_user_at = str(previous_user_at)[:19] if previous_user_at else None
        cur = conn.execute(
            "INSERT INTO chat_messages (author,content,image_url,file_url,file_name) "
            "VALUES (?,?,?,?,?)",
            (author, content, image_url, file_url, file_name),
        )
        message_id = cur.lastrowid
        if _user_events_requested:
            row = conn.execute(
                "SELECT created_at FROM chat_messages WHERE id=?",
                (message_id,),
            ).fetchone()
            if row is not None:
                created_at = row['created_at'] if hasattr(row, 'keys') else row[0]
                created_at = str(created_at)[:19] if created_at else None
        capture_alert_failed = False
        # Shadow user_rule outbox：与 chat INSERT 同事务；缺表则 outbox_capture_gap
        # 聊天主流程不得阻断；证据全失败时记 sticky alert（status fail-closed）
        if (
            _user_events_requested
            and author not in ('fyodor', 'assistant', 'claude')
            and message_id is not None
            and created_at
        ):
            try:
                import internal_state_shadow as _shadow
                if _shadow.is_user_events_enabled():
                    try:
                        _shadow.enqueue_user_rule_in_txn(
                            conn,
                            message_id=int(message_id),
                            text=content or '',
                            created_at=created_at,
                            previous_user_at=previous_user_at,
                        )
                    except Exception:
                        try:
                            _shadow.mark_proof_gap(
                                conn,
                                failed_message_id=int(message_id),
                                error_code='outbox_capture_gap',
                                db_path=DB_PATH,
                            )
                        except Exception:
                            try:
                                _gap_result = _shadow.mark_proof_gap_standalone(
                                    db_path=DB_PATH,
                                    failed_message_id=int(message_id),
                                    error_code='outbox_capture_gap',
                                )
                                if _gap_result.status == 'failed':
                                    capture_alert_failed = not _shadow.note_capture_evidence_failure(
                                        f'user_rule message_id={message_id}',
                                        db_path=DB_PATH,
                                    )
                            except Exception as _gap_exc:
                                capture_alert_failed = not _shadow.note_capture_evidence_failure(
                                    f'user_rule standalone exception={_gap_exc}',
                                    db_path=DB_PATH,
                                )
            except Exception as _cap_exc:
                try:
                    import internal_state_shadow as _shadow2
                    capture_alert_failed = not _shadow2.note_capture_evidence_failure(
                        f'user_rule import/enable: {_cap_exc}',
                        db_path=DB_PATH,
                    )
                except Exception:
                    try:
                        from internal_state_capture_alert import write_capture_alert
                        capture_alert_failed = not write_capture_alert(
                            db_path=DB_PATH,
                            detail=f'user_rule shadow import/enable: {_cap_exc}',
                        )
                    except Exception:
                        capture_alert_failed = True
        conn.commit()
    finally:
        conn.close()
    # Production React path: app persists here, then gateway stream only gets
    # user_message_id (empty content). Touch must happen on this commit —
    # moments_turn.insert_user_message will not run again for the body.
    # Text / image / file messages all count as real interaction.
    if author not in ('fyodor', 'assistant', 'claude'):
        try:
            from chat.interaction_state import touch_user_interaction
            touch_user_interaction(get_db)
        except Exception:
            pass
        if message_id is not None and created_at:
            try:
                import internal_state_shadow as _shadow
                _shadow.drain_shadow_outbox_best_effort(db_path=DB_PATH)
            except Exception:
                pass
    # gateway uses this id to claim wake context only after a successful reply.
    return jsonify({"ok": True, "message_id": message_id})

@app.route('/api/chat/reply', methods=['POST'])
def chat_reply():
    import requests as req
    conn = get_db()
    rows = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    rows = list(reversed(rows))
    messages = []
    for r in rows:
        role = 'assistant' if r['author'] in ('fyodor','claude','assistant') else 'user'
        content = r['content'] or ''
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'] += '\n' + content
        else:
            messages.append({'role':role,'content':content})
    if not messages or messages[0]['role'] == 'assistant':
        messages.insert(0,{'role':'user','content':'...'})
    try:
        persona = open('/opt/frontend/prompts/persona.md').read()
    except:
        persona = '你是费奥多尔，一个渊博冷静却深情的人。'
    now = datetime.datetime.utcnow()+datetime.timedelta(hours=8)
    system = f"{persona}\n\n当前时间：{now.strftime('%Y-%m-%d %H:%M')}"
    from relay.manager import RelayManager
    from chat.response_parser import extract_text
    rm = RelayManager()
    result = rm.call({'max_tokens':1024,'system':system,'messages':messages}, timeout=120)
    text = extract_text(result)
    if not text:
        return jsonify({"error":"empty response"}),500
    conn = get_db()
    conn.execute("INSERT INTO chat_messages (author,content) VALUES ('fyodor',?)",(text,))
    conn.commit()
    conn.close()
    return jsonify({"ok":True,"content":text})

@app.route('/api/diary/generate', methods=['POST'])
def diary_generate():
    import sys
    sys.path.insert(0, '/opt/frontend')
    import auto_diary
    try:
        import importlib; importlib.reload(auto_diary)
        if auto_diary.today_diary_exists():
            return jsonify({"ok": False, "message": "今天已经写过日记啦 📖"})
        result = auto_diary.generate()
        if result is None:
            return jsonify({"ok": False, "message": "今天没有聊天记录，跳过日记生成。"})
        return jsonify({"ok": True, "content": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ── Block / Settings ──

@app.route('/api/status/blocked')
def status_blocked():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='blocked'").fetchone()
    conn.close()
    blocked = (row['value'] == 'true') if row else False
    return jsonify({"blocked": blocked})

@app.route('/api/block', methods=['POST'])
def toggle_block():
    if request.headers.get('X-Admin') != 'true':
        return jsonify({"error": "unauthorized"}), 403
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='blocked'").fetchone()
    new_val = 'false' if (row and row['value'] == 'true') else 'true'
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('blocked',?)", (new_val,))
    conn.commit()
    conn.close()
    return jsonify({"blocked": new_val == 'true'})

# ── Drift Bottles ──

@app.route('/api/drift/bottles')
def drift_bottles_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM drift_bottles ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify({"bottles": [dict(r) for r in rows]})

@app.route('/api/drift/send', methods=['POST'])
def drift_send():
    import random
    data = request.get_json()
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({"error": "empty"}), 400
    minutes = random.randint(30, 1440)
    found_at = (datetime.datetime.utcnow() + datetime.timedelta(hours=8, minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    conn.execute("INSERT INTO drift_bottles (content,found_at) VALUES (?,?)", (content, found_at))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "found_in_minutes": minutes})

@app.route('/api/drift/check')
def drift_check():
    import requests as req
    now_str = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    bottles = conn.execute(
        "SELECT * FROM drift_bottles WHERE status='floating' AND found_at <= ?", (now_str,)
    ).fetchall()
    results = []
    for b in bottles:
        try:
            persona = open('/opt/frontend/prompts/persona.md').read()
            now_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            system = (persona + "\n\n当前时间：" + now_dt.strftime('%Y-%m-%d %H:%M') +
                      "\n\n哈娅给你写了一封漂流瓶。请以费奥多尔的口吻回复，简短有温度。")
            from relay.manager import RelayManager
            from chat.response_parser import extract_text
            rm = RelayManager()
            result = rm.call({'max_tokens':512,'system':system,
                      'messages':[{'role':'user','content':b['content']}]}, timeout=60)
            text = extract_text(result)
            conn.execute("UPDATE drift_bottles SET status='found', reply=? WHERE id=?", (text, b['id']))
            conn.commit()
            results.append({'id': b['id'], 'ok': True})
        except Exception as e:
            results.append({'id': b['id'], 'error': str(e)})
    conn.close()
    return jsonify({"checked": len(bottles), "results": results})

@app.route('/api/drift/found')
def drift_found():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM drift_bottles WHERE status='found' ORDER BY found_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify({"bottles": [dict(r) for r in rows]})

@app.route('/api/drift/mine')
def drift_mine():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM drift_bottles ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify({"bottles": [dict(r) for r in rows]})



# ── Mijia light setup & proxy ──
import urllib.request as _urlreq
import urllib.error as _urlerr
LIGHT_DAEMON = 'http://127.0.0.1:5052'

@app.route('/setup/mijia')
def setup_mijia():
    html = """<!DOCTYPE html><html lang=zh><head><meta charset=UTF-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>米家授权</title>
<style>
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
font-family:-apple-system,'PingFang SC',sans-serif;background:#f8f8f6;color:#2a2020;gap:22px;padding:30px;}
.card{background:#fff;border-radius:20px;padding:30px;box-shadow:0 8px 28px rgba(0,0,0,.08);text-align:center;max-width:320px;}
h1{font-size:18px;font-weight:500;margin:0 0 6px;}
p{font-size:13px;color:#9a8a8a;margin:0 0 18px;line-height:1.7;}
img{width:240px;height:240px;border-radius:12px;background:#f0ebf4;object-fit:contain;}
.tip{font-size:11px;color:#b8b0b8;margin-top:14px;}
</style></head><body>
<div class=card>
<h1>米家授权</h1>
<p>用米家 App 扫码授权<br>授权次卧灯的控制权限</p>
<img src="/static/qrcode.png?t=" id="qr" alt="二维码加载中…">
<div class=tip>二维码 2 分钟内有效，过期请重新运行登录脚本</div>
</div>
<script>
// 二维码可能稍后才生成，定时刷新
function refresh(){document.getElementById('qr').src='/static/qrcode.png?t='+Date.now();}
refresh();setInterval(refresh,5000);
</script>
</body></html>"""
    return html

@app.route('/api/light/<path:action>', methods=['GET','POST'])
def light_proxy(action):
    url = f"{LIGHT_DAEMON}/light/{action}"
    body = request.get_data() if request.method == 'POST' else None
    req = _urlreq.Request(url, data=body, method=request.method,
                          headers={'Content-Type': 'application/json'})
    try:
        with _urlreq.urlopen(req, timeout=15) as resp:
            return resp.read(), resp.status, {'Content-Type': 'application/json'}
    except _urlerr.HTTPError as e:
        return e.read(), e.code, {'Content-Type': 'application/json'}
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

# ── Core page ──

@app.route('/core')
def core_page():
    return send_from_directory('/opt/frontend/static', 'core.html')

# ── Settings key/value ──

@app.route('/api/settings/<key>', methods=['GET'])
def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "value": None}), 404
    return jsonify({"ok": True, "value": row['value']})

@app.route('/api/settings/<key>', methods=['POST'])
def set_setting(key):
    data = request.get_json()
    value = data.get('value', '')
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Persona read/write ──

@app.route('/api/persona', methods=['GET'])
def get_persona():
    try:
        text = open('/opt/frontend/prompts/persona.md').read()
        return jsonify({"ok": True, "content": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/persona', methods=['POST'])
def save_persona():
    import subprocess
    data = request.get_json()
    content = data.get('content', '')
    try:
        open('/opt/frontend/prompts/persona.md', 'w').write(content)
        subprocess.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── User Profile (chatnest-compatible) ──

@app.route('/api/profile', methods=['GET'])
def get_user_profile():
    import user_profile as _up
    try:
        return jsonify({"ok": True, "profile": _up.read_profile()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/profile', methods=['PUT'])
def put_user_profile():
    import user_profile as _up
    data = request.get_json(silent=True) or {}
    payload = data.get('profile', data)
    try:
        profile = _up.write_profile(payload)
        return jsonify({"ok": True, "profile": profile})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Posts PATCH (tags / resolved) ──

@app.route('/api/posts/<int:pid>', methods=['PATCH'])
def patch_post(pid):
    data = request.get_json()
    conn = get_db()
    if 'tags' in data:
        conn.execute("UPDATE posts SET tags=? WHERE id=?", (data['tags'], pid))
    if 'resolved' in data:
        conn.execute("UPDATE posts SET resolved=? WHERE id=?", (int(data['resolved']), pid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── Long-term / Recent pages ──

@app.route('/long-term')
def long_term_page():
    return send_from_directory('/opt/frontend/static', 'long-term.html')

@app.route('/recent')
def recent_page():
    return send_from_directory('/opt/frontend/static', 'recent.html')

# ── Posts PUT (content / tags only) ──

@app.route('/api/posts/<int:pid>', methods=['PUT'])
def update_post(pid):
    data = request.get_json()
    conn = get_db()
    if 'content' in data:
        c = str(data['content']).strip()
        if c:
            conn.execute("UPDATE posts SET content=? WHERE id=?", (c, pid))
    if 'tags' in data:
        conn.execute("UPDATE posts SET tags=? WHERE id=?", (data['tags'], pid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Co-Reading book routes ──

import json as _json, glob as _glob

CO_DATA = '/opt/co-reading/data'

def _read_json(p, fallback=None):
    try:
        return _json.loads(open(p).read())
    except Exception:
        return fallback

def _book_colors(bid):
    h = sum(ord(x) for x in bid) % 360
    return f'hsl({h},35%,72%)', f'hsl({(h+40)%360},30%,60%)'

@app.route('/api/books', methods=['GET'])
def books_list():
    books = []
    for mf in _glob.glob(f'{CO_DATA}/books/*/manifest.json'):
        m = _read_json(mf)
        if m:
            prog = _read_json(f'{CO_DATA}/progress.json', {})
            bp = prog.get(m['bookId'], {})
            total = len(m.get('chunks', []))
            read = len(bp.get('readChunkIds', []))
            c1, c2 = _book_colors(m['bookId'])
            books.append({
                'bookId': m['bookId'], 'title': m['title'],
                'author': m.get('author',''), 'total': total,
                'read': read, 'color1': c1, 'color2': c2,
                'lastReadAt': bp.get('lastReadAt'),
            })
    books.sort(key=lambda x: x.get('lastReadAt') or '', reverse=True)
    return jsonify({'books': books})

@app.route('/api/books/current', methods=['GET'])
def books_current():
    prog = _read_json(f'{CO_DATA}/progress.json', {})
    if not prog:
        return jsonify({'book': None})
    bid = max(prog, key=lambda k: prog[k].get('lastReadAt',''))
    m = _read_json(f'{CO_DATA}/books/{bid}/manifest.json')
    if not m:
        return jsonify({'book': None})
    bp = prog.get(bid, {})
    total = len(m.get('chunks', []))
    read = len(bp.get('readChunkIds', []))
    c1, c2 = _book_colors(bid)
    return jsonify({'book': {
        'bookId': bid, 'title': m['title'], 'author': m.get('author',''),
        'total': total, 'read': read, 'color1': c1, 'color2': c2,
        'lastChunkId': bp.get('lastChunkId'),
        'progress': round(read / total * 100) if total else 0,
    }})

@app.route('/api/books/<book_id>/chunks', methods=['GET'])
def book_chunks(book_id):
    m = _read_json(f'{CO_DATA}/books/{book_id}/manifest.json')
    if not m:
        return jsonify({'error': 'not found'}), 404
    prog = _read_json(f'{CO_DATA}/progress.json', {})
    read_ids = set(prog.get(book_id, {}).get('readChunkIds', []))
    chunks = [{**ch, 'read': ch['id'] in read_ids} for ch in m.get('chunks', [])]
    return jsonify({'chunks': chunks, 'title': m['title']})

@app.route('/api/books/<book_id>/chunks/<chunk_id>', methods=['GET'])
def book_chunk(book_id, chunk_id):
    m = _read_json(f'{CO_DATA}/books/{book_id}/manifest.json')
    if not m:
        return jsonify({'error': 'not found'}), 404
    chunk_meta = next((ch for ch in m.get('chunks',[]) if ch['id']==chunk_id), None)
    if not chunk_meta:
        return jsonify({'error': 'chunk not found'}), 404
    try:
        text = open(f"{CO_DATA}/books/{book_id}/{chunk_meta['path']}").read()
    except Exception:
        return jsonify({'error': 'file not found'}), 404
    return jsonify({'chunk': chunk_meta, 'text': text, 'bookTitle': m['title']})

@app.route('/api/books/<book_id>/annotations', methods=['GET'])
def book_annotations(book_id):
    rows = []
    try:
        for line in open(f'{CO_DATA}/annotations.jsonl'):
            line = line.strip()
            if not line:
                continue
            obj = _json.loads(line)
            if obj.get('bookId') == book_id:
                rows.append(obj)
    except FileNotFoundError:
        pass
    return jsonify({'annotations': rows})

@app.route('/api/books/<book_id>/annotations', methods=['POST'])
def create_annotation(book_id):
    import uuid, datetime as _dt
    data = request.get_json()
    ann = {
        'id': str(uuid.uuid4()),
        'bookId': book_id,
        'chunkId': data.get('chunkId',''),
        'quote': data.get('quote',''),
        'kind': data.get('kind','highlight'),
        'author': data.get('author','haya'),
        'note': data.get('note',''),
        'paragraphIdx': data.get('paragraphIdx', 0),
        'createdAt': _dt.datetime.utcnow().isoformat() + 'Z',
    }
    with open(f'{CO_DATA}/annotations.jsonl', 'a') as f:
        f.write(_json.dumps(ann, ensure_ascii=False) + '\n')
    return jsonify({'annotation': ann}), 201

@app.route('/api/books/<book_id>/progress', methods=['POST'])
def update_book_progress(book_id):
    import datetime as _dt
    data = request.get_json()
    prog = _read_json(f'{CO_DATA}/progress.json', {})
    bp = prog.get(book_id, {'readChunkIds': []})
    chunk_id = data.get('chunkId')
    if chunk_id and chunk_id not in bp['readChunkIds']:
        bp['readChunkIds'].append(chunk_id)
    bp['lastChunkId'] = chunk_id or bp.get('lastChunkId')
    bp['lastReadAt'] = _dt.datetime.utcnow().isoformat() + 'Z'
    prog[book_id] = bp
    open(f'{CO_DATA}/progress.json','w').write(_json.dumps(prog, ensure_ascii=False, indent=2))
    return jsonify({'ok': True})

@app.route('/read')
def read_page():
    return send_from_directory('/opt/frontend/static', 'read.html')

@app.route('/reader')
def reader_page():
    return send_from_directory('/opt/frontend/static', 'reader.html')


# ── API config routes ──

def _active_relay_id():
    return (config_store.get('ACTIVE_RELAY', '') or '').strip()


def _effective_relay_model():
    active_id = _active_relay_id()
    if active_id:
        try:
            conn = get_db()
            row = conn.execute('SELECT default_model FROM relay_presets WHERE id=?', (active_id,)).fetchone()
            conn.close()
            if row and (row['default_model'] or '').strip():
                return row['default_model'].strip()
        except Exception:
            pass
    return config_store.get('MODEL') or 'unknown'


@app.route('/api/config/model', methods=['GET'])
def config_get_model():
    return jsonify({'model': _effective_relay_model(), 'global_model': config_store.get('MODEL') or ''})

@app.route('/api/config/model', methods=['POST'])
def config_set_model():
    data = request.get_json()
    new_model = (data.get('model') or '').strip()
    if not new_model:
        return jsonify({'error': 'empty model'}), 400
    try:
        active_id = _active_relay_id()
        if active_id:
            conn = get_db()
            conn.execute('UPDATE relay_presets SET default_model=? WHERE id=?', (new_model, active_id))
            conn.commit()
            conn.close()
            return jsonify({'ok': True, 'model': new_model, 'scope': 'active_relay'})
        config_store.set('MODEL', new_model)
        return jsonify({'ok': True, 'model': new_model, 'scope': 'global'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/model-catalog', methods=['GET'])
def config_model_catalog():
    """策展过的模型清单（models.json）+ 当前生效模型。
    id 是 relay 的真实模型名；thinking 字段（extended/none）决定 gateway 是否传 thinking 参数。"""
    try:
        with open('/opt/frontend/models.json') as f:
            catalog = json.load(f)
    except Exception:
        catalog = []
    return jsonify({'models': catalog, 'current': _effective_relay_model() or ''})

@app.route('/api/config/key-status', methods=['GET'])
def config_key_status():
    import datetime as _dt
    from relay.manager import RelayManager
    rm = RelayManager()
    key = rm.api_key
    masked = (key[:8] + '···' + key[-4:]) if len(key) > 12 else '***'
    today = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%Y-%m-%d')
    conn = get_db()
    row = conn.execute(
        "SELECT count(*) FROM chat_messages WHERE created_at >= ? AND created_at < ?",
        (today + ' 00:00:00', today + ' 23:59:59')
    ).fetchone()
    conn.close()
    # 实际生效的 relay（跟随 ACTIVE_RELAY，不是死读 .env）
    return jsonify({'masked_key': masked, 'today_msgs': row[0] if row else 0,
                    'source': rm.api_url, 'raw_len': len(key)})

@app.route('/api/config/key', methods=['POST'])
def config_set_key():
    data = request.get_json()
    new_key = (data.get('key') or '').strip()
    if not new_key:
        return jsonify({'error': 'empty key'}), 400
    try:
        active_relay_id = config_store.get('ACTIVE_RELAY', '')
        if active_relay_id:
            # 当前在用某个预设，改它的 key（改 .env 不会生效，relay.manager 优先读预设）
            conn = get_db()
            conn.execute('UPDATE relay_presets SET key=? WHERE id=?', (new_key, active_relay_id))
            conn.commit()
            conn.close()
        else:
            # 还没选过预设，走部署期默认值
            env = open('/opt/frontend/.env').read()
            env2 = re.sub(r'^ANTHROPIC_API_KEY=.*$',
                          f'ANTHROPIC_API_KEY={new_key}', env, flags=re.MULTILINE)
            open('/opt/frontend/.env', 'w').write(env2)
        # 不需要重启：relay.manager 每次请求都重新读，立即生效
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config/test-send', methods=['POST'])
def config_test_send():
    import urllib.error as _ue
    data = request.get_json()
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({'error': 'empty'}), 400
    try:
        from relay.manager import RelayManager
        from chat.response_parser import extract_text
        rm = RelayManager()
        result = rm.call({
            'max_tokens': 512,
            'messages': [{'role': 'user', 'content': msg}],
        }, timeout=60)
        text = extract_text(result)
        usage = result.get('usage', {})
        tokens = usage.get('input_tokens', 0) + usage.get('output_tokens', 0)
        return jsonify({'text': text, 'tokens': tokens, 'model': rm.model})
    except _ue.HTTPError as e:
        body = e.read().decode(errors='replace')
        return jsonify({'error': f'HTTP {e.code}: {body[:200]}'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api-test')
def api_test_page():
    return send_from_directory('/opt/frontend/static', 'api-test.html')

def _gw_json_request(method, path, body=None, timeout=5):
    """Call frontend-gw (5051) from app.py for repair utilities."""
    import json as _json
    import urllib.error
    import urllib.request
    url = 'http://127.0.0.1:5051' + path
    data = _json.dumps(body).encode('utf-8') if body is not None else None
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', 'ignore')
            return _json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', 'ignore')
        try:
            return _json.loads(raw) if raw else {'error': exc.code}
        except Exception:
            return {'error': raw or str(exc)}
    except Exception as exc:
        return {'error': str(exc)}


def _gw_gen_lock_status():
    data = _gw_json_request('GET', '/chat/lock')
    if isinstance(data, dict) and 'busy' in data:
        return data
    return {'busy': None, 'age_sec': 0, 'error': data.get('error', 'unreachable')}


def _gw_unlock_gen_lock(rounds=4):
    """Force-release gen lock; repeat for multi-worker gunicorn."""
    last = {}
    for _ in range(max(1, int(rounds))):
        last = _gw_json_request('POST', '/chat/cancel', {'force': True})
    lock = _gw_gen_lock_status()
    return {'cancel': last, 'lock': lock, 'ok': not lock.get('busy')}


@app.route('/repair')
def repair_page():
    return send_from_directory('/opt/frontend/static', 'repair.html')

@app.route('/api/repair/key')
def repair_key():
    key = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('DEEPSEEK_API_KEY='):
                key = line.split('=',1)[1].strip()
    except: pass
    return jsonify({'key': key})

@app.route('/api/repair/chat', methods=['POST'])
def repair_chat_api():
    data = request.get_json() or {}
    message = (data.get('message') or '').strip()
    history = data.get('history') or []
    if not message:
        return jsonify({'error': 'empty message'}), 400
    try:
        from tools.repair_agent import repair_chat
        reply, tool_log = repair_chat(message, history)
        return jsonify({'reply': reply, 'tools': tool_log})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/repair/status')
def repair_status():
    import socket
    def port_open(p):
        try:
            s = socket.create_connection(('127.0.0.1', p), timeout=1)
            s.close(); return True
        except: return False
    return jsonify({
        'port_5050': port_open(5050),
        'port_5051': port_open(5051),
        'port_5056': port_open(5056),
        'port_8000': port_open(8000),
        'gen_lock': _gw_gen_lock_status(),
    })


@app.route('/api/repair/unlock-gen', methods=['POST'])
def repair_unlock_gen():
    """Force-release chat generation lock (multi-worker safe-ish)."""
    return jsonify(_gw_unlock_gen_lock())


# ── EPUB upload & import ──

@app.route('/api/books/upload', methods=['POST'])
def upload_epub():
    import subprocess, tempfile, os as _os, json as _json
    if 'file' not in request.files:
        return jsonify({'error': 'no file field'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'empty filename'}), 400
    # save to temp file
    tmp = tempfile.NamedTemporaryFile(suffix='.epub', delete=False)
    f.save(tmp.name)
    tmp.close()
    try:
        result = subprocess.run(
            ['python3', '/opt/co-reading/scripts/import_epub.py',
             tmp.name,
             '--out', '/opt/co-reading/data/books'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return jsonify({'error': result.stderr.strip() or 'import failed'}), 500
        book_dir = result.stdout.strip()
        # read manifest to return metadata
        mf_path = _os.path.join(book_dir, 'manifest.json')
        mf = _json.loads(open(mf_path).read()) if _os.path.exists(mf_path) else {}
        return jsonify({
            'ok': True,
            'bookId': mf.get('bookId', ''),
            'title': mf.get('title', ''),
            'author': mf.get('author', ''),
            'chunks': len(mf.get('chunks', [])),
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'import timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            _os.unlink(tmp.name)
        except Exception:
            pass


def _env_set(key, value):
    path = '/opt/frontend/.env'
    lines = open(path).read().splitlines()
    found = False
    for i, ln in enumerate(lines):
        if ln.startswith(key + '='):
            lines[i] = key + '=' + value
            found = True
            break
    if not found:
        lines.append(key + '=' + value)
    open(path, 'w').write('\n'.join(lines) + '\n')

@app.route('/api/config/provider', methods=['GET'])
def config_get_provider():
    provider = config_store.get('GW_PROVIDER', 'api_relay')
    has_token = False
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
                has_token = bool(line.split('=', 1)[1].strip())
    except Exception:
        pass
    return jsonify({'provider': provider, 'cc_token_set': has_token})

@app.route('/api/config/provider', methods=['POST'])
def config_set_provider():
    data = request.get_json() or {}
    provider = (data.get('provider') or '').strip()
    if provider not in ('api_relay', 'claude_code'):
        return jsonify({'error': 'provider must be api_relay or claude_code'}), 400
    try:
        config_store.set('GW_PROVIDER', provider)
        return jsonify({'ok': True, 'provider': provider})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/cc-token', methods=['POST'])
def config_set_cc_token():
    import subprocess
    data = request.get_json() or {}
    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'empty token'}), 400
    try:
        _env_set('CLAUDE_CODE_OAUTH_TOKEN', token)
        subprocess.Popen(['systemctl', 'restart', 'frontend-gw'])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Monitor tables init ──
def _init_monitor_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS bugs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','+8 hours')),
        resolved INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS fixes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now','+8 hours'))
    )""")
    conn.commit()
    conn.close()

_init_monitor_tables()

# ── Monitor API ──
@app.route('/api/monitor/services', methods=['GET'])
def monitor_services():
    import subprocess
    svcs = ['frontend', 'frontend-gw', 'mcp-http', 'ombre-brain', 'mijia-light', 'co-reading']
    result = {}
    for s in svcs:
        try:
            r = subprocess.run(['systemctl', 'is-active', s], capture_output=True, text=True, timeout=3)
            result[s] = r.stdout.strip() == 'active'
        except Exception:
            result[s] = False
    return jsonify(result)

@app.route('/api/monitor/logs', methods=['GET'])
def monitor_logs():
    import subprocess, re as _re
    units = ['frontend', 'frontend-gw', 'mcp-http', 'ombre-brain', 'mijia-light', 'co-reading']
    args = []
    for u in units:
        args += ['-u', u + '.service']
    try:
        r = subprocess.run(
            ['journalctl'] + args + ['-n', '20', '--no-pager', '--output=short'],
            capture_output=True, text=True, timeout=6
        )
        raw = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith('--')]
        logs = []
        for line in raw[-20:]:
            m = _re.match(r'\w{3}\s+\d+\s+[\d:]+\s+\S+\s+(.*)', line)
            text = (m.group(1) if m else line)[:110]
            ll = text.lower()
            if any(w in ll for w in ['error', 'traceback', 'exception', 'failed', 'crit', '500']):
                level = 'error'
            elif any(w in ll for w in ['200', '201', 'started', 'running', 'active', 'ok', 'success']):
                level = 'success'
            else:
                level = 'info'
            logs.append({'text': text, 'level': level})
        return jsonify({'logs': logs})
    except Exception as e:
        return jsonify({'logs': [{'text': str(e), 'level': 'error'}]})

@app.route('/api/monitor/bugs', methods=['GET'])
def get_bugs():
    conn = get_db()
    rows = conn.execute("SELECT id,content,created_at,resolved FROM bugs ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify({'bugs': [dict(r) for r in rows]})

@app.route('/api/monitor/bugs', methods=['POST'])
def add_bug():
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'empty'}), 400
    conn = get_db()
    conn.execute("INSERT INTO bugs (content) VALUES (?)", (content,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/monitor/fixes', methods=['GET'])
def get_fixes():
    conn = get_db()
    rows = conn.execute("SELECT id,content,created_at FROM fixes ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify({'fixes': [dict(r) for r in rows]})

@app.route('/api/monitor/fixes', methods=['POST'])
def add_fix():
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'empty'}), 400
    conn = get_db()
    conn.execute("INSERT INTO fixes (content) VALUES (?)", (content,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/monitor/patrol', methods=['POST'])
def trigger_patrol():
    import subprocess as _sp
    try:
        r = _sp.run(
            ['python3', '/opt/frontend/tools/patrol.py'],
            capture_output=True, text=True, timeout=90
        )
        output = r.stdout.strip() or r.stderr.strip() or '(no output)'
        ok = r.returncode == 0
        return jsonify({'ok': ok, 'result': output})
    except _sp.TimeoutExpired:
        return jsonify({'error': 'timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Period Tracker ──
def _init_period_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS period_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        type TEXT NOT NULL,
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','+8 hours'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS period_days (
        date TEXT PRIMARY KEY,
        data TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT DEFAULT (datetime('now','+8 hours'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS period_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    conn.commit()
    conn.close()

_init_period_tables()

@app.route('/api/period/records', methods=['GET'])
def get_period_records():
    year  = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    date  = request.args.get('date', '')
    conn  = get_db()
    if date:
        rows = conn.execute(
            "SELECT * FROM period_records WHERE date=? ORDER BY id", (date,)
        ).fetchall()
    elif year and month:
        prefix = f"{year}-{month:02d}"
        rows = conn.execute(
            "SELECT * FROM period_records WHERE date LIKE ? ORDER BY date,id",
            (prefix + '%',)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM period_records ORDER BY date DESC LIMIT 200"
        ).fetchall()
    conn.close()
    return jsonify({'records': [dict(r) for r in rows]})

@app.route('/api/period/records', methods=['POST'])
def add_period_record():
    data  = request.get_json() or {}
    date  = (data.get('date') or '').strip()
    rtype = (data.get('type') or '').strip()
    note  = (data.get('note') or '').strip()
    if not date or rtype not in ('period', 'sex'):
        return jsonify({'error': 'invalid'}), 400
    conn = get_db()
    cur  = conn.execute(
        "INSERT INTO period_records (date,type,note) VALUES (?,?,?)", (date, rtype, note)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': cur.lastrowid})

@app.route('/api/period/records/<int:rid>', methods=['DELETE'])
def delete_period_record(rid):
    conn = get_db()
    conn.execute("DELETE FROM period_records WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/period/stats', methods=['GET'])
def period_stats():
    from datetime import datetime as _dt, timedelta as _td
    conn = get_db()
    rows = conn.execute(
        "SELECT date FROM period_records WHERE type='period' ORDER BY date"
    ).fetchall()
    conn.close()
    dates = [r['date'] for r in rows]
    if not dates:
        return jsonify({'last_period': None, 'cycle_length': None,
                        'next_period': None, 'ovulation': None})
    last = dates[-1]
    cycle_length = 28
    if len(dates) >= 2:
        diffs = []
        for i in range(1, len(dates)):
            d1 = _dt.strptime(dates[i-1], '%Y-%m-%d')
            d2 = _dt.strptime(dates[i], '%Y-%m-%d')
            diff = (d2 - d1).days
            if 18 <= diff <= 45:
                diffs.append(diff)
        if diffs:
            cycle_length = round(sum(diffs) / len(diffs))
    last_dt  = _dt.strptime(last, '%Y-%m-%d')
    next_dt  = last_dt + _td(days=cycle_length)
    ovul_dt  = next_dt - _td(days=14)
    return jsonify({
        'last_period':   last,
        'cycle_length':  cycle_length,
        'next_period':   next_dt.strftime('%Y-%m-%d'),
        'ovulation':     ovul_dt.strftime('%Y-%m-%d'),
    })


# 每日详细记录：{came, flow, pain, states[], extras[], sex, note}
# 写入时同步维护 period_records（came→type='period'，sex→type='sex'），
# 这样 /api/period/stats 和其他读 period_records 的端不受影响。
@app.route('/api/period/days', methods=['GET'])
def get_period_days():
    month = (request.args.get('month') or '').strip()  # YYYY-MM
    conn = get_db()
    if month:
        rows = conn.execute(
            "SELECT date,data FROM period_days WHERE date LIKE ? ORDER BY date",
            (month + '%',)
        ).fetchall()
        legacy = conn.execute(
            "SELECT date,type FROM period_records WHERE date LIKE ?", (month + '%',)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT date,data FROM period_days ORDER BY date DESC LIMIT 400"
        ).fetchall()
        legacy = conn.execute(
            "SELECT date,type FROM period_records ORDER BY date DESC LIMIT 400"
        ).fetchall()
    conn.close()
    # 旧表记录先铺底（'period'/'start' 视为经期日，'sex' 为亲密），
    # 这样新页面能看到历史数据；period_days 里的记录覆盖同一天。
    days = {}
    for r in legacy:
        d = days.setdefault(r['date'], {})
        if r['type'] in ('period', 'start'):
            d['came'] = True
        elif r['type'] == 'sex':
            d['sex'] = True
    for r in rows:
        try:
            rec = json.loads(r['data'])
        except (ValueError, TypeError):
            continue
        if isinstance(rec, dict):
            days[r['date']] = rec
    return jsonify({'days': days})


def _sync_period_record(conn, date, rtype, present):
    exists = conn.execute(
        "SELECT id FROM period_records WHERE date=? AND type=?", (date, rtype)
    ).fetchone()
    if present and not exists:
        conn.execute("INSERT INTO period_records (date,type) VALUES (?,?)", (date, rtype))
    elif not present and exists:
        conn.execute("DELETE FROM period_records WHERE date=? AND type=?", (date, rtype))


@app.route('/api/period/day', methods=['PUT', 'POST'])
def put_period_day():
    data = request.get_json() or {}
    date = (data.get('date') or '').strip()
    record = data.get('record')
    if not date or not isinstance(record, dict):
        return jsonify({'error': 'invalid'}), 400
    allowed = {'came', 'flow', 'pain', 'states', 'extras', 'sex', 'note'}
    record = {k: v for k, v in record.items() if k in allowed and v is not None}
    conn = get_db()
    if record:
        conn.execute(
            """INSERT INTO period_days (date,data,updated_at)
               VALUES (?,?,datetime('now','+8 hours'))
               ON CONFLICT(date) DO UPDATE SET
                 data=excluded.data, updated_at=excluded.updated_at""",
            (date, json.dumps(record, ensure_ascii=False))
        )
    else:
        conn.execute("DELETE FROM period_days WHERE date=?", (date,))
    _sync_period_record(conn, date, 'period', record.get('came') is True)
    _sync_period_record(conn, date, 'sex', record.get('sex') is True)
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'date': date, 'record': record})


@app.route('/api/period/settings', methods=['GET'])
def get_period_settings():
    conn = get_db()
    rows = conn.execute("SELECT key,value FROM period_settings").fetchall()
    conn.close()
    kv = {r['key']: r['value'] for r in rows}
    def _int(k):
        try:
            return int(kv[k])
        except (KeyError, ValueError):
            return None
    return jsonify({
        'cycle_length':  _int('cycle_length'),
        'period_length': _int('period_length'),
        'last_start':    kv.get('last_start') or None,
    })


@app.route('/api/period/settings', methods=['PUT', 'POST'])
def put_period_settings():
    data = request.get_json() or {}
    conn = get_db()
    for key, lo, hi in (('cycle_length', 21, 40), ('period_length', 2, 10)):
        v = data.get(key)
        if isinstance(v, (int, float)) and lo <= int(v) <= hi:
            conn.execute(
                "INSERT INTO period_settings (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(int(v)))
            )
    ls = (data.get('last_start') or '').strip() if isinstance(data.get('last_start'), str) else ''
    if ls:
        conn.execute(
            "INSERT INTO period_settings (key,value) VALUES ('last_start',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ls,)
        )
    conn.commit()
    conn.close()
    return get_period_settings()



@app.route('/api/brain/emotions', methods=['GET', 'PATCH'])
def brain_emotions_proxy():
    if request.method == 'PATCH':
        try:
            require_owner(request)
        except OwnerAuthError as exc:
            resp = jsonify({'ok': False, 'error': exc.message})
            resp.status_code = exc.status_code
            if exc.status_code == 401:
                resp.headers['WWW-Authenticate'] = 'Bearer'
            return resp
        payload = request.get_json() or {}
        path = (payload.get('path') or '').strip()
        if not path:
            return jsonify({'ok': False, 'error': 'path required'}), 400
        try:
            valence = float(payload.get('valence'))
            arousal = float(payload.get('arousal'))
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid valence or arousal'}), 400
        try:
            import emotion_memories as _em
            item = _em.update_memory_point(path, valence, arousal)
            return jsonify({'ok': True, 'item': item})
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 500
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 500

    try:
        import emotion_memories as _em
        items = _em.list_memory_points(limit=15)
        return jsonify({'ok': True, 'items': items})
    except RuntimeError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

@app.route('/api/brain/emotion_history', methods=['GET'])
def brain_emotion_history():
    try:
        days = int(request.args.get('days', 7))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'invalid days'}), 400
    if days not in (7, 30):
        days = 7 if days < 30 else 30
    try:
        import emotion_history as _eh
        series = _eh.fetch_series(days, db_path=DB_PATH)
        return jsonify({'ok': True, 'days': days, 'series': series})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/brain/dreams', methods=['GET'])
def brain_dreams_proxy():
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
        page = fetch_dream_page(conn, limit=limit, before=before, include_id=True)
        conn.close()
        for item in page['items']:
            item['created_at'] = moments_store.to_iso8601_shanghai(item.get('created_at'))
            if item.get('created_at'):
                item['date'] = item['created_at'][:10]
        return jsonify({
            'ok': True,
            'items': page['items'],
            'has_more': page['has_more'],
            'next_before': page['next_before'],
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

def _brain_posts_page(type_name: str, *, limit_default=20, limit_max=50, order_by='id'):
    """通用 posts 分页：limit + before(id)。返回 (items_rows, has_more, next_before)。"""
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
            # before = 上一页最后一条 id（仍用 id 游标，避免同秒歧义）
            rows = conn.execute(
                f"SELECT id, author, content, created_at FROM posts "
                f"WHERE type=? AND id < ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (type_name, before, limit + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id, author, content, created_at FROM posts "
                f"WHERE type=? ORDER BY created_at DESC, id DESC LIMIT ?",
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
def brain_thoughts_proxy():
    try:
        rows, has_more, next_before, err = _brain_posts_page('THOUGHT', limit_default=20)
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        items = []
        for r in rows:
            created_at = moments_store.to_iso8601_shanghai(r['created_at'])
            items.append({
                'id': int(r['id']),
                'author': (r['author'] or 'fyodor').strip() or 'fyodor',
                'created_at': created_at,
                'time': created_at[11:16] if created_at else '-',
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
def brain_diary_proxy():
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
            created_at = moments_store.to_iso8601_shanghai(r['created_at'])
            items.append({
                'id': int(r['id']),
                'author': (r['author'] or 'fyodor').strip() or 'fyodor',
                'created_at': created_at,
                'date': created_at[:10] if created_at else '\u2014',
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


@app.route('/api/tools/drawers', methods=['GET', 'PATCH'])
def tools_drawers():
    import tool_drawers
    if request.method == 'PATCH':
        try:
            require_owner(request)
        except OwnerAuthError as exc:
            resp = jsonify({'ok': False, 'error': exc.message})
            resp.status_code = exc.status_code
            if exc.status_code == 401:
                resp.headers['WWW-Authenticate'] = 'Bearer'
            return resp
        payload = request.get_json() or {}
        enabled_flag = payload.get('enabled')
        if not isinstance(enabled_flag, bool):
            return jsonify({'ok': False, 'error': 'enabled must be boolean'}), 400
        try:
            if payload.get('tool'):
                tool_drawers.set_tool_enabled(str(payload['tool']), enabled_flag)
            elif payload.get('drawer_id'):
                tool_drawers.set_drawer_enabled(str(payload['drawer_id']), enabled_flag)
            else:
                return jsonify({'ok': False, 'error': 'tool or drawer_id required'}), 400
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        return jsonify({
            'ok': True,
            'enabled': config_store.get('TOOL_DRAWERS_ENABLED', '0') == '1',
            'drawers': tool_drawers.serialize_drawers(),
        })

    enabled = config_store.get('TOOL_DRAWERS_ENABLED', '0') == '1'
    return jsonify({'ok': True, 'enabled': enabled, 'drawers': tool_drawers.serialize_drawers()})


@app.route('/api/config/relay', methods=['POST'])
def config_relay():
    """直接指定一个不在预设列表里的 url/key（老接口，保留兼容）。
    清空 ACTIVE_RELAY 让 relay.manager 回退到这里写的部署期默认值，
    不然预设机制会覆盖掉这里改的东西，改了不生效。"""
    data = request.get_json() or {}
    new_url = (data.get('url') or '').strip()
    new_key = (data.get('key') or '').strip()
    if not new_url:
        return jsonify({'error': 'url required'}), 400
    try:
        env = open('/opt/frontend/.env').read()
        if 'API_URL=' in env:
            env = re.sub(r'^API_URL=.*$', f'API_URL={new_url}', env, flags=re.MULTILINE)
        else:
            env = env.rstrip() + f'\nAPI_URL={new_url}\n'
        if new_key:
            if 'ANTHROPIC_API_KEY=' in env:
                env = re.sub(r'^ANTHROPIC_API_KEY=.*$', f'ANTHROPIC_API_KEY={new_key}', env, flags=re.MULTILINE)
            else:
                env = env.rstrip() + f'\nANTHROPIC_API_KEY={new_key}\n'
        open('/opt/frontend/.env', 'w').write(env)
        config_store.set('ACTIVE_RELAY', '')  # 回退到 .env 默认值，不然预设覆盖这次改动
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/models', methods=['GET'])
def config_models():
    import urllib.request as _ur, urllib.error as _ue, json as _j
    from relay.manager import RelayManager
    rm = RelayManager()
    api_url, key = rm.api_url, rm.api_key
    if not api_url:
        return jsonify({'error': 'API_URL not set'}), 400
    # Derive models URL: replace /messages at end with /models, or replace path
    models_url = re.sub(r'/messages$', '/models', api_url)
    if models_url == api_url:
        # Try base/v1/models
        models_url = re.sub(r'/v1/.*$', '/v1/models', api_url)
    req = _ur.Request(models_url, headers={
        'x-api-key': key,
        'Authorization': f'Bearer {key}',
        'anthropic-version': '2023-06-01',
    })
    try:
        with _ur.urlopen(req, timeout=15) as resp:
            raw = _j.loads(resp.read())
        ids = []
        def _extract(lst):
            for item in (lst or []):
                if isinstance(item, str) and item:
                    ids.append(item)
                elif isinstance(item, dict):
                    mid = item.get('id') or item.get('name') or item.get('model_id') or ''
                    if mid:
                        ids.append(mid)
        # Anthropic format: {"data": [...]}
        if isinstance(raw, dict) and raw.get('data'):
            _extract(raw['data'])
        # Alternative: {"models": [...]}
        if not ids and isinstance(raw, dict) and raw.get('models'):
            _extract(raw['models'])
        # Alternative: {"model_list": [...]}
        if not ids and isinstance(raw, dict) and raw.get('model_list'):
            _extract(raw['model_list'])
        # Alternative: direct array
        if not ids and isinstance(raw, list):
            _extract(raw)
        # Last resort: values of top-level dict
        if not ids and isinstance(raw, dict):
            _extract([v for v in raw.values() if isinstance(v, (str, dict))])
        # Filter obviously non-model entries
        ids = [m for m in ids if m and not m.startswith('{')]
        return jsonify({'ok': True, 'models': ids})
    except _ue.HTTPError as e:
        body = e.read().decode(errors='replace')
        return jsonify({'error': f'HTTP {e.code}: {body[:200]}'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _init_relay_presets_table():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS relay_presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        key TEXT,
        created_at DATETIME DEFAULT (datetime('now','+8 hours'))
    )''')
    # 幂等补列：default_model / capabilities 可能是历史上手动 ALTER 加的
    cols = {r[1] for r in conn.execute('PRAGMA table_info(relay_presets)').fetchall()}
    if 'default_model' not in cols:
        conn.execute("ALTER TABLE relay_presets ADD COLUMN default_model TEXT DEFAULT ''")
    if 'capabilities' not in cols:
        conn.execute("ALTER TABLE relay_presets ADD COLUMN capabilities TEXT DEFAULT ''")
    if 'status_url' not in cols:
        conn.execute("ALTER TABLE relay_presets ADD COLUMN status_url TEXT DEFAULT ''")
    conn.commit()
    conn.close()


def _init_relay_account_credentials_table():
    _init_relay_presets_table()
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS relay_account_credentials (
        preset_id INTEGER PRIMARY KEY,
        user_id TEXT NOT NULL,
        credential_kind TEXT NOT NULL,
        secret_ciphertext TEXT NOT NULL,
        created_at DATETIME DEFAULT (datetime('now','+8 hours')),
        updated_at DATETIME DEFAULT (datetime('now','+8 hours'))
    )''')
    conn.commit()
    conn.close()

@app.route('/api/config/relay-presets', methods=['GET'])
def get_relay_presets():
    _init_relay_account_credentials_table()
    # 判断"使用中"：ACTIVE_RELAY（runtime_config，存 preset id）优先；
    # 还没设置过 ACTIVE_RELAY 时（迁移期）回退按 .env 的 API_URL 匹配。
    active_relay_id = config_store.get('ACTIVE_RELAY', '')
    active_url = ''
    if not active_relay_id:
        try:
            for line in open('/opt/frontend/.env'):
                if line.startswith('API_URL='):
                    active_url = line.split('=', 1)[1].strip()
        except Exception:
            pass
    conn = get_db()
    rows = conn.execute('''
        SELECT r.id,r.name,r.url,r.key,r.default_model,r.capabilities,r.status_url,r.created_at,
               CASE WHEN c.preset_id IS NULL THEN 0 ELSE 1 END AS account_balance_configured,
               COALESCE(c.credential_kind, '') AS account_credential_kind
        FROM relay_presets r
        LEFT JOIN relay_account_credentials c ON c.preset_id=r.id
        ORDER BY r.created_at
    ''').fetchall()
    conn.close()
    from relay.capabilities import get_caps as _get_caps
    presets = []
    for r in rows:
        caps_raw = (r['capabilities'] or '').strip()
        if caps_raw:
            try:
                caps = json.loads(caps_raw)
            except Exception:
                caps = _get_caps(r['url'])
        else:
            # 未手动设置过 → 按 URL 自动检测，作为展示用默认值（不写库）
            caps = _get_caps(r['url'])
        if active_relay_id:
            is_active = str(r['id']) == str(active_relay_id)
        else:
            is_active = r['url'] == active_url
        presets.append({
            'id': r['id'], 'name': r['name'], 'url': r['url'],
            'active': is_active,
            'default_model': r['default_model'] or '',
            'capabilities': caps,
            'status_url': r['status_url'] or '',
            'account_balance_configured': bool(r['account_balance_configured']),
            'account_credential_kind': r['account_credential_kind'] or '',
        })
    return jsonify({'ok': True, 'presets': presets, 'active_url': active_url})

@app.route('/api/config/relay-presets', methods=['POST'])
def add_relay_preset():
    _init_relay_presets_table()
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    url = (data.get('url') or '').strip()
    key = (data.get('key') or '').strip()
    if not name or not url:
        return jsonify({'error': 'name and url required'}), 400
    caps_in = data.get('capabilities')
    if isinstance(caps_in, dict):
        caps_json = json.dumps({
            'thinking': bool(caps_in.get('thinking', True)),
            'cache': bool(caps_in.get('cache', True)),
            'tools': bool(caps_in.get('tools', True)),
        })
    else:
        caps_json = ''  # 未提供 → 前端展示时按 URL 自动检测
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO relay_presets (name, url, key, default_model, capabilities, status_url) VALUES (?,?,?,?,?,?)',
        (name, url, key, (data.get('default_model') or '').strip(), caps_json,
         (data.get('status_url') or '').strip())
    )
    conn.commit()
    preset_id = cur.lastrowid
    conn.close()
    return jsonify({'ok': True, 'id': preset_id})


@app.route('/api/config/relay-presets/<int:preset_id>/intelligence', methods=['GET'])
def inspect_relay_preset(preset_id):
    """Return model, pricing and health metadata without returning the API key."""
    _init_relay_presets_table()
    _init_relay_account_credentials_table()
    conn = get_db()
    row = conn.execute(
        '''
        SELECT r.id,r.name,r.url,r.key,r.status_url,
               c.user_id,c.credential_kind,c.secret_ciphertext
        FROM relay_presets r
        LEFT JOIN relay_account_credentials c ON c.preset_id=r.id
        WHERE r.id=?
        ''',
        (preset_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404

    from relay.channel_intelligence import ChannelInspectionError, inspect_channel
    console_kwargs = {}
    if row['secret_ciphertext']:
        try:
            from relay.credential_vault import CredentialVaultError, decrypt_secret
            console_kwargs = {
                'console_credential_kind': row['credential_kind'] or '',
                'console_credential_secret': decrypt_secret(row['secret_ciphertext']),
                'console_user_id': row['user_id'] or '',
            }
        except CredentialVaultError:
            # 凭据损坏时仍可匿名拉模型/状态，只是价格可能显示需登录
            console_kwargs = {}
    try:
        result = inspect_channel(
            {
                'id': row['id'],
                'name': row['name'],
                'base_url': row['url'],
                'api_key': row['key'] or '',
                'status_url': row['status_url'] or '',
            },
            include_status=request.args.get('status', '1') != '0',
            force=request.args.get('refresh', '0') == '1',
            **console_kwargs,
        )
        return jsonify(result)
    except ChannelInspectionError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/config/relay-presets/<int:preset_id>/balance', methods=['GET'])
def get_relay_preset_balance(preset_id):
    """Query the saved API key's NewAPI limit without returning the key."""
    _init_relay_presets_table()
    conn = get_db()
    row = conn.execute(
        'SELECT id,name,url,key FROM relay_presets WHERE id=?',
        (preset_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404

    from relay.channel_intelligence import ChannelInspectionError, query_channel_balance
    try:
        balance = query_channel_balance({
            'id': row['id'],
            'name': row['name'],
            'base_url': row['url'],
            'api_key': row['key'] or '',
        })
        return jsonify({'ok': True, 'balance': balance})
    except ChannelInspectionError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


from account_balance_routes import create_relay_account_blueprint
app.register_blueprint(create_relay_account_blueprint(
    get_db=get_db,
    ensure_tables=_init_relay_account_credentials_table,
))

@app.route('/api/config/relay-presets/<int:preset_id>', methods=['DELETE'])
def delete_relay_preset(preset_id):
    _init_relay_account_credentials_table()
    conn = get_db()
    conn.execute('DELETE FROM relay_account_credentials WHERE preset_id=?', (preset_id,))
    conn.execute('DELETE FROM relay_presets WHERE id=?', (preset_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/config/relay-presets/<int:preset_id>/activate', methods=['POST'])
def activate_relay_preset(preset_id):
    _init_relay_presets_table()
    conn = get_db()
    row = conn.execute('SELECT * FROM relay_presets WHERE id=?', (preset_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    try:
        # ACTIVE_RELAY 存 relay_presets.id，url/key 由 relay.manager 按这个 id 实时查表
        config_store.set('ACTIVE_RELAY', str(preset_id))
        new_model = (row['default_model'] or '').strip()
        # 不写 .env，不重启进程；模型也跟随 preset，不再污染全局 MODEL。
        return jsonify({'ok': True, 'model_switched': new_model or None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _init_todos_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS todos ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'content TEXT NOT NULL, '
        'done INTEGER DEFAULT 0, '
        'due_date TEXT, '
        'author TEXT, '
        "created_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_todos_table()


# -- To-Do List --

@app.route('/api/todos', methods=['GET'])
def get_todos():
    conn = get_db()
    undone = conn.execute(
        'SELECT * FROM todos WHERE done=0 ORDER BY '
        "CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END, "
        'due_date ASC, id ASC'
    ).fetchall()
    done = conn.execute(
        'SELECT * FROM todos WHERE done=1 ORDER BY id DESC LIMIT 5'
    ).fetchall()
    conn.close()
    return jsonify({'todos': [dict(r) for r in undone] + [dict(r) for r in done]})

@app.route('/api/todos', methods=['POST'])
def add_todo():
    data = request.get_json() or {}
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    due_date = (data.get('due_date') or '').strip() or None
    author   = (data.get('author')   or '').strip() or None
    conn = get_db()
    conn.execute('INSERT INTO todos (content, due_date, author) VALUES (?,?,?)',
        (content, due_date, author))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/todos/<int:tid>/toggle', methods=['POST'])
def toggle_todo(tid):
    conn = get_db()
    conn.execute('UPDATE todos SET done = 1 - done WHERE id=?', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/todos/<int:tid>', methods=['PATCH'])
def patch_todo(tid):
    data = request.get_json() or {}
    done = data.get('done')
    if done is None:
        return jsonify({'error': 'done required'}), 400
    conn = get_db()
    conn.execute('UPDATE todos SET done=? WHERE id=?', (1 if bool(done) else 0, tid))
    row = conn.execute('SELECT * FROM todos WHERE id=?', (tid,)).fetchone()
    conn.commit(); conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify(dict(row))

@app.route('/api/todos/<int:tid>', methods=['DELETE'])
def delete_todo(tid):
    conn = get_db()
    conn.execute('DELETE FROM todos WHERE id=?', (tid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── 位置上报 ──────────────────────────────────────────────────
@app.route('/api/geo/report', methods=['POST'])
def geo_report():
    data = request.get_json() or {}
    lat_wgs = data.get('lat')
    lon_wgs = data.get('lon')
    accuracy = data.get('accuracy', 0)
    coord_type = (data.get('coord_type') or data.get('crs') or '').strip().lower() or None
    if coord_type in ('gcj', 'gcj02', 'amap'):
        coord_type = 'gcj02'
    elif coord_type in ('wgs', 'wgs84', 'gps'):
        coord_type = 'wgs84'
    if not lat_wgs or not lon_wgs:
        return jsonify({'error': 'missing lat/lon'}), 400
    import sys
    if '/opt/frontend' not in sys.path:
        sys.path.insert(0, '/opt/frontend')
    from tools.geo_utils import resolve_location
    loc = resolve_location(lat_wgs, lon_wgs, coord_type=coord_type)
    conn = get_db()
    conn.execute(
        'INSERT INTO geo_log (lat_wgs,lon_wgs,lat_gcj,lon_gcj,accuracy,address,poi,city) VALUES (?,?,?,?,?,?,?,?)',
        (
            loc['lat_wgs'], loc['lon_wgs'], loc['lat_gcj'], loc['lon_gcj'],
            float(accuracy), loc['address'], loc['poi'], loc['city'],
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({
        'ok': True,
        'address': loc['address'],
        'poi': loc['poi'],
        'city': loc['city'],
        'coord_src': loc.get('coord_src'),
    })

@app.route('/api/geo/latest', methods=['GET'])
def geo_latest():
    conn = get_db()
    row = conn.execute('SELECT * FROM geo_log ORDER BY id DESC LIMIT 1').fetchone()
    conn.close()
    if not row: return jsonify({'ok':False,'error':'no data'})
    return jsonify({'ok':True,**dict(row)})


# ── 手机监控上报（电量/屏幕时长 + 截屏链路）───────────────────────
def _init_phone_monitor_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS device_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        battery_percent INTEGER,
        battery_charging INTEGER,
        charge_type TEXT,
        temp_c REAL,
        screen_today_minutes INTEGER,
        created_at DATETIME DEFAULT (datetime('now','+8 hours'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS screenshot_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT DEFAULT 'tool',
        note TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',  -- pending / dispatched / done
        requested_at DATETIME DEFAULT (datetime('now','+8 hours')),
        dispatched_at DATETIME,
        completed_at DATETIME
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS screenshot_captures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER,
        attachment_id TEXT NOT NULL,
        capture_ts_ms INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT (datetime('now','+8 hours'))
    )""")
    conn.commit()
    conn.close()


_init_phone_monitor_tables()


@app.route('/api/device/report', methods=['POST'])
def device_report():
    d = request.get_json() or {}
    conn = get_db()
    conn.execute(
        "INSERT INTO device_status (battery_percent,battery_charging,charge_type,temp_c,screen_today_minutes) "
        "VALUES (?,?,?,?,?)",
        (
            int(d.get('battery_percent', -1) or -1),
            int(d.get('battery_charging', 0) or 0),
            str(d.get('charge_type', 'none') or 'none')[:16],
            float(d.get('temp_c', -1) or -1),
            int(d.get('screen_today_minutes', -1) or -1),
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/device/latest', methods=['GET'])
def device_latest():
    conn = get_db()
    row = conn.execute(
        "SELECT *, CAST((julianday('now','+8 hours')-julianday(created_at))*86400 AS INT) AS age_sec "
        "FROM device_status ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'ok': False, 'error': 'no data'})
    d = dict(row)
    d['age_sec'] = max(0, int(d.get('age_sec') or 0))
    return jsonify({'ok': True, **d})


@app.route('/api/screenshot/request', methods=['POST'])
def screenshot_request():
    d = request.get_json() or {}
    source = (d.get('source') or 'tool').strip()[:32]
    note = (d.get('note') or '').strip()[:200]
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO screenshot_requests (source,note,status,requested_at) "
        "VALUES (?,?, 'pending', datetime('now','+8 hours'))",
        (source, note)
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return jsonify({'ok': True, 'request_id': rid})


@app.route('/api/screenshot/upload', methods=['POST'])
def screenshot_upload():
    raw = request.get_data() or b''
    if not raw:
        return jsonify({'ok': False, 'error': 'empty body'}), 400
    tmp = '/tmp/screen_' + uuid.uuid4().hex + '.jpg'
    with open(tmp, 'wb') as f:
        f.write(raw)
    try:
        aid = attachment_store.save(tmp, kind='image', mime='image/jpeg')
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(e)}), 500

    ts = request.headers.get('X-Capture-Ts', '').strip()
    try:
        capture_ts_ms = int(ts) if ts else 0
    except Exception:
        capture_ts_ms = 0

    conn = get_db()
    req = conn.execute(
        "SELECT id FROM screenshot_requests "
        "WHERE status IN ('pending','dispatched') "
        "ORDER BY CASE status WHEN 'dispatched' THEN 0 ELSE 1 END, id DESC LIMIT 1"
    ).fetchone()
    req_id = int(req['id']) if req else None
    if req_id:
        conn.execute(
            "UPDATE screenshot_requests SET status='done', completed_at=datetime('now','+8 hours') WHERE id=?",
            (req_id,)
        )
    conn.execute(
        "INSERT INTO screenshot_captures (request_id, attachment_id, capture_ts_ms) VALUES (?,?,?)",
        (req_id, aid, capture_ts_ms)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'attachment': 'attachment://' + aid, 'request_id': req_id})


@app.route('/api/screenshot/latest', methods=['GET'])
def screenshot_latest():
    after_req = request.args.get('after_request_id', type=int)
    conn = get_db()
    if after_req:
        row = conn.execute(
            "SELECT c.*, CAST((julianday('now','+8 hours')-julianday(c.created_at))*86400 AS INT) AS age_sec "
            "FROM screenshot_captures c WHERE c.request_id >= ? ORDER BY c.id DESC LIMIT 1",
            (after_req,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT c.*, CAST((julianday('now','+8 hours')-julianday(c.created_at))*86400 AS INT) AS age_sec "
            "FROM screenshot_captures c ORDER BY c.id DESC LIMIT 1"
        ).fetchone()
    conn.close()
    if not row:
        return jsonify({'ok': False, 'error': 'no data'})
    d = dict(row)
    d['age_sec'] = max(0, int(d.get('age_sec') or 0))
    d['attachment'] = 'attachment://' + d['attachment_id']
    return jsonify({'ok': True, **d})


# ── 附件间接层：attachment://<id> 的唯一取图入口 ──────────────
@app.route('/api/attachments/<aid>', methods=['GET'])
def get_attachment(aid):
    a = attachment_store.get(aid)
    if not a:
        abort(404)  # 不存在或已过期（生命周期删掉了）——优雅 404
    return send_from_directory(attachment_store.ATTACH_DIR, a['filename'],
                               mimetype=a.get('mime') or 'image/png')


# ── Gallery（收藏相册）─────────────────────────────────────────
@app.route('/gallery')
def gallery_page():
    return send_from_directory('/opt/frontend/static', 'gallery.html')

@app.route('/api/gallery/photo/<pid>', methods=['GET'])
def gallery_photo(pid):
    p = gallery_store.get(pid)
    if not p:
        abort(404)
    return send_from_directory(gallery_store.GALLERY_DIR, p['storage_key'],
                               mimetype=p.get('mime') or 'image/png')

@app.route('/api/gallery/albums', methods=['GET'])
def gallery_albums():
    return jsonify({'albums': gallery_store.list_albums()})

@app.route('/api/gallery/photos', methods=['GET'])
def gallery_photos_list():
    album_id = request.args.get('album_id', type=int)
    q = (request.args.get('q') or '').strip()
    fav = request.args.get('favorite') in ('1', 'true')
    if q:
        photos = gallery_store.search_photos(q, limit=120)
    elif fav:
        photos = gallery_store.list_photos(favorite_only=True, limit=300)
    else:
        photos = gallery_store.list_photos(album_id=album_id, limit=300)
    import json as _json
    def _kw(p):
        try:
            return _json.loads(p.get('keywords') or '[]')
        except Exception:
            return []
    # 只对外暴露需要的字段（不暴露 storage_key 物理路径）
    out = [{'pid': p['pid'], 'album_id': p['album_id'], 'note': p['note'],
            'width': p['width'], 'height': p['height'], 'favorite': p['favorite'],
            'created_at': p['created_at'], 'saved_at': p['saved_at'],
            'source_type': p['source_type'],
            'summary': p.get('summary') or '', 'emotion': p.get('emotion') or '',
            'keywords': _kw(p), 'importance': p.get('importance') or 0} for p in photos]
    return jsonify({'photos': out})

# ── Gallery 收藏层：打理照片（星标/改备注/移动/删除/新建相册）────────
@app.route('/api/gallery/photo/<pid>/favorite', methods=['POST'])
def gallery_favorite(pid):
    v = gallery_store.toggle_favorite(pid)
    if v is None:
        abort(404)
    return jsonify({'ok': True, 'favorite': v})

@app.route('/api/gallery/photo/<pid>/update', methods=['POST'])
def gallery_update(pid):
    d = request.get_json() or {}
    note = d.get('note')
    album_id = d.get('album_id')
    gallery_store.update_photo(pid, note=note,
                               album_id=int(album_id) if album_id else None)
    return jsonify({'ok': True})

@app.route('/api/gallery/photo/<pid>/delete', methods=['POST'])
def gallery_delete(pid):
    deleted, storage_key, mem_id = moments_store.delete_gallery_item_with_social(
        pid,
        memories_db_path=DB_PATH,
        gallery_db_path=gallery_store.DB_PATH,
    )
    if not deleted:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    if storage_key:
        try:
            os.remove(os.path.join(gallery_store.GALLERY_DIR, storage_key))
        except OSError:
            pass
    if mem_id:
        try:
            conn = get_db()
            conn.execute("DELETE FROM posts WHERE id=? AND type='PHOTO'", (mem_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
    return jsonify({'ok': True})

@app.route('/api/gallery/album', methods=['POST'])
def gallery_create_album():
    d = request.get_json() or {}
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'name required'}), 400
    aid = gallery_store.album_by_name(name) or gallery_store.create_album(name, d.get('description', ''))
    return jsonify({'ok': True, 'album_id': aid})


# ── Moments 朋友圈封面 ─────────────────────────────────────────
MOMENTS_COVER_META = os.path.join(UPLOAD_DIR, 'moments_cover.meta.json')


def _moments_cover_read_meta():
    try:
        with open(MOMENTS_COVER_META, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _moments_cover_url():
    meta = _moments_cover_read_meta()
    fname = (meta.get('file') or '').strip()
    if not fname:
        return None
    path = os.path.join(UPLOAD_DIR, fname)
    if not os.path.isfile(path):
        return None
    return f'/static/uploads/{fname}'


@app.route('/api/moments/cover', methods=['GET'])
def moments_cover_get():
    url = _moments_cover_url()
    return jsonify({'ok': True, 'url': url})


@app.route('/api/moments/cover', methods=['POST'])
def moments_cover_upload():
    try:
        require_owner(request)
    except OwnerAuthError as exc:
        resp = jsonify({'ok': False, 'error': exc.message})
        resp.status_code = exc.status_code
        if exc.status_code == 401:
            resp.headers['WWW-Authenticate'] = 'Bearer'
        return resp
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'no file'}), 400
    f = request.files['file']
    try:
        raw = moments_cover.read_bounded(f.stream)
        data = moments_cover.encode_cover_image(raw)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = moments_cover.output_extension()
    fname = f'moments_cover_{uuid.uuid4().hex[:10]}{ext}'
    with open(os.path.join(UPLOAD_DIR, fname), 'wb') as out:
        out.write(data)
    old = _moments_cover_read_meta().get('file')
    meta = {'file': fname, 'updated_at': datetime.datetime.utcnow().isoformat() + 'Z'}
    with open(MOMENTS_COVER_META, 'w', encoding='utf-8') as out:
        json.dump(meta, out, ensure_ascii=False)
    if old and old != fname:
        try:
            os.remove(os.path.join(UPLOAD_DIR, old))
        except OSError:
            pass
    url = f'/static/uploads/{fname}'
    return jsonify({'ok': True, 'url': url})


# ── 倒计时任务浮窗 ─────────────────────────────────────────────
@app.route('/api/commands/pending', methods=['GET'])
def commands_pending():
    return jsonify({'commands': command_store.list_pending()})

@app.route('/api/commands/<int:cid>/started', methods=['POST'])
def command_started(cid):
    command_store.mark_started(cid)
    return jsonify({'ok': True})

@app.route('/api/commands/<int:cid>/done', methods=['POST'])
def command_done(cid):
    r = command_store.mark_done(cid)
    return jsonify({'ok': True, **(r or {})})

@app.route('/api/commands/<int:cid>/cancel', methods=['POST'])
def command_cancel(cid):
    command_store.mark_canceled(cid)
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=False)

# ── Dream Events (感知层 Phase 1) ──────────────────────────
def _init_dream_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS dream_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        value TEXT,
        created_at TIMESTAMP DEFAULT (datetime('now','+8 hours'))
    )""")
    # dream_pool 线上手建：只增列，不改既有列
    try:
        pool_cols = [r[1] for r in conn.execute('PRAGMA table_info(dream_pool)').fetchall()]
        if pool_cols and 'metadata' not in pool_cols:
            conn.execute('ALTER TABLE dream_pool ADD COLUMN metadata TEXT')
    except Exception:
        pass
    # 私人梦史：潜梦库（幂等）
    conn.execute("""CREATE TABLE IF NOT EXISTS dream_latents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        content TEXT,
        origin TEXT,
        source_id INTEGER,
        valence REAL,
        arousal REAL,
        recurrence INTEGER DEFAULT 0,
        last_used_at TEXT,
        created_at TEXT DEFAULT (datetime('now', '+8 hours'))
    )""")
    conn.commit()
    conn.close()

_init_dream_tables()

@app.route('/api/dream/events', methods=['GET'])
def log_dream_event():
    etype = request.args.get('type', '').strip()
    value = request.args.get('value', '').strip()
    if not etype:
        return jsonify({'error': 'type required'}), 400
    conn = get_db()
    now_str = "datetime('now','+8 hours')"

    # 5分钟内同 type 已有记录则跳过（去重）
    existing = conn.execute(
        """SELECT id FROM dream_events
           WHERE type=? AND created_at >= datetime('now','+8 hours','-5 minutes')
           ORDER BY id DESC LIMIT 1""",
        (etype,)
    ).fetchone()
    if existing:
        conn.close()
        return '', 200

    # 计算上一条记录（不同type）距今的时长，作为"上一个app的使用时长"写回去
    prev = conn.execute(
        """SELECT id, created_at FROM dream_events
           WHERE type != ? ORDER BY id DESC LIMIT 1""",
        (etype,)
    ).fetchone()
    if prev:
        try:
            from datetime import datetime
            prev_time = datetime.strptime(prev['created_at'], '%Y-%m-%d %H:%M:%S')
            now_time = datetime.utcnow().replace(tzinfo=None)
            # created_at已经是+8小时，now也需要+8
            import datetime as _dt
            now_bj = (_dt.datetime.utcnow() + _dt.timedelta(hours=8))
            diff_minutes = round((now_bj - prev_time).total_seconds() / 60, 1)
            # 只写入合理范围内的时长（1分钟~4小时），过短或过长都忽略
            if 1 <= diff_minutes <= 240:
                conn.execute(
                    "UPDATE dream_events SET duration_minutes=? WHERE id=?",
                    (diff_minutes, prev['id'])
                )
        except Exception:
            pass

    conn.execute(
        "INSERT INTO dream_events (type, value, created_at) VALUES (?,?,datetime('now','+8 hours'))",
        (etype, value)
    )
    conn.commit()
    conn.close()
    return '', 200

# ── Wake Log (Phase 2) ─────────────────────────────────────
def _init_wake_tables():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS wake_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        woke_at TIMESTAMP DEFAULT (datetime('now','+8 hours')),
        thoughts TEXT,
        action TEXT,
        content TEXT,
        consumed INTEGER DEFAULT 0,
        cache_info TEXT DEFAULT ''
    )""")
    try:
        conn.execute("ALTER TABLE wake_log ADD COLUMN notified INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE wake_log ADD COLUMN surfaced_desire_ids TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE wake_log ADD COLUMN cache_info TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE wake_log ADD COLUMN wake_run_id TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_wake_log_run_id "
            "ON wake_log(wake_run_id) WHERE wake_run_id IS NOT NULL AND wake_run_id != ''"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN cache_info TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

_init_wake_tables()

@app.route('/api/wake_log/pending_notification', methods=['GET'])
def pending_notification():
    """供VII app轮询：是否有费奥多尔自主发出的、还没推送过的消息。
    取最新一条未推送的 action='message'，并把所有未推送的一并标记，
    避免她隔几小时打开时被一堆补发的旧通知刷屏。
    另外复用这个通道下发 command='screenshot' 让手机立刻截一张屏。"""
    conn = get_db()
    row = conn.execute(
        "SELECT id, content, woke_at FROM wake_log "
        "WHERE action='message' AND (notified IS NULL OR notified=0) "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE wake_log SET notified=1 WHERE action='message' AND (notified IS NULL OR notified=0)"
        )

    # 手机截屏指令：优先取 pending；若 dispatched 超过 2 分钟还没回传，重试下发一次
    sreq = conn.execute(
        "SELECT id FROM screenshot_requests "
        "WHERE status='pending' "
        "   OR (status='dispatched' AND dispatched_at < datetime('now','+8 hours','-2 minutes')) "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if sreq:
        conn.execute(
            "UPDATE screenshot_requests SET status='dispatched', dispatched_at=datetime('now','+8 hours') "
            "WHERE id=?",
            (int(sreq['id']),)
        )

    conn.commit()
    conn.close()

    if not row and not sreq:
        return jsonify({'has_message': False})

    payload = {'has_message': bool(row)}
    if row:
        payload.update({
            'id': 'wake-' + str(row['id']),
            'title': '费奥多尔',
            'content': row['content'],
            'woke_at': row['woke_at'],
        })
    if sreq:
        payload.update({
            'command': 'screenshot',
            'command_id': 'cap-' + str(int(sreq['id'])),
        })
    return jsonify(payload)

# ── Board 留言板 ───────────────────────────────────────────
@app.route('/board')
def board_page():
    return send_from_directory('/opt/frontend/static', 'board.html')

@app.route('/api/board', methods=['GET'])
def get_board():
    status_f = request.args.get('status', '').strip()
    tag_f    = request.args.get('tag', '').strip()
    conn = get_db()
    where, params = [], []
    if status_f:
        where.append("status=?"); params.append(status_f)
    if tag_f:
        tags = [t.strip() for t in tag_f.split(',') if t.strip()]
        where.append(f"tag IN ({','.join('?'*len(tags))})"); params.extend(tags)
    sql = "SELECT * FROM board" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        replies = conn.execute(
            "SELECT * FROM board_replies WHERE board_id=? ORDER BY created_at ASC", (row['id'],)
        ).fetchall()
        item = dict(row); item['replies'] = [dict(r) for r in replies]
        try:
            item['meta'] = json.loads(item.get('meta') or '{}')
        except Exception:
            item['meta'] = {}
        result.append(item)
    conn.close()
    return jsonify(result)

@app.route('/api/board', methods=['POST'])
def post_board():
    # ── 留言板写入说明（给新窗口的费佳看）──────────────────────────────
    # 留言板现在是三板块运维面板，写入时指定 tab 字段：
    #
    #   tab='patrol'    → 巡逻报告（P0/P1警报，cc处理后标done）
    #   tab='changelog' → 修建日志（谁改了什么、为什么、改了哪些文件）
    #   tab='status'    → 当前状态（正在做的功能，参考meta.state）
    #
    # meta 字段（JSON对象，不是字符串）：
    #   patrol:    {"priority": "P0"}
    #   changelog: {"type": "fix|feature|refactor", "files": ["gateway.py"]}
    #   status:    {"state": "active|pending|planned|done", "owner": "cc", "category": "前端"}
    #
    # title 字段：一句话标题，列表页显示用（不填则截取content前30字）
    # status 字段：'open'（未处理）/ 'done'（已处理）
    # ────────────────────────────────────────────────────────────────────
    data = request.get_json() or {}
    author  = (data.get('author') or 'hayana').strip()
    if author == 'fyodor':
        if not BOARD_TOKEN_FYODOR or data.get('token','') != BOARD_TOKEN_FYODOR:
            return jsonify({'error': 'unauthorized'}), 403
    tag     = (data.get('tag') or '闲聊').strip()
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    level    = (data.get('level') or '').strip() or None
    category = (data.get('category') or '给活儿').strip()
    if level and level not in ('P0', 'P1', 'P2'):
        return jsonify({'error': 'level must be P0/P1/P2'}), 400
    if category not in ('给活儿', '播报'):
        category = '给活儿'
    mentions = (data.get('mentions') or '').strip()
    tab   = (data.get('tab') or 'patrol').strip()
    if tab not in ('patrol', 'changelog', 'status'):
        tab = 'patrol'
    title = (data.get('title') or '').strip()
    meta_in = data.get('meta')
    if isinstance(meta_in, dict):
        meta = json.dumps(meta_in, ensure_ascii=False)
    elif isinstance(meta_in, str) and meta_in.strip():
        meta = meta_in  # 已经是 JSON 字符串，原样存
    else:
        meta = '{}'
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO board (author,tag,content,status,level,category,mentions,tab,title,meta) "
        "VALUES (?,?,?,'open',?,?,?,?,?,?)",
        (author, tag, content, level, category, mentions, tab, title, meta)
    )
    conn.commit(); new_id = cur.lastrowid; conn.close()

    # @fyodor_cc → 立刻触发cc_board_check，不等巡逻周期
    if 'fyodor_cc' in mentions:
        import subprocess, os, sys as _sys
        subprocess.Popen(
            [_sys.executable, '/opt/frontend/tools/cc_board_check.py'],
            cwd='/opt/frontend',
            env={**os.environ, 'HOME': '/root'},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    return jsonify({'ok': True, 'id': new_id})

@app.route('/api/board/<int:bid>/reply', methods=['POST'])
def post_board_reply(bid):
    data    = request.get_json() or {}
    author  = (data.get('author') or 'hayana').strip()
    if author == 'fyodor':
        if not BOARD_TOKEN_FYODOR or data.get('token','') != BOARD_TOKEN_FYODOR:
            return jsonify({'error': 'unauthorized'}), 403
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    mentions = (data.get('mentions') or '').strip()
    # done 没传时默认 None（不动 status），传了才按值处理——
    # 之前这个参数一直被忽略，回复时想顺手标记已处理的调用全部没生效。
    done_in = data.get('done')
    conn = get_db()
    conn.execute(
        "INSERT INTO board_replies (board_id,author,content,mentions) VALUES (?,?,?,?)",
        (bid, author, content, mentions)
    )
    if done_in is not None:
        if done_in:
            conn.execute(
                "UPDATE board SET status='done', resolved_at=datetime('now','+8 hours') WHERE id=?",
                (bid,)
            )
        else:
            conn.execute("UPDATE board SET status='open', resolved_at=NULL WHERE id=?", (bid,))
    conn.commit(); conn.close()

    # 回复里@fyodor_cc → 同样立刻触发
    if 'fyodor_cc' in mentions:
        import subprocess, os, sys as _sys
        subprocess.Popen(
            [_sys.executable, '/opt/frontend/tools/cc_board_check.py'],
            cwd='/opt/frontend',
            env={**os.environ, 'HOME': '/root'},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    return jsonify({'ok': True})

@app.route('/api/board/<int:bid>/status', methods=['POST'])
def update_board_status(bid):
    data   = request.get_json() or {}
    status = (data.get('status') or 'open').strip()
    if status not in ('open', 'done'):
        return jsonify({'error': 'invalid status'}), 400
    conn = get_db()
    if status == 'done':
        conn.execute(
            "UPDATE board SET status=?, resolved_at=datetime('now','+8 hours') WHERE id=?",
            (status, bid)
        )
    else:
        conn.execute("UPDATE board SET status=?, resolved_at=NULL WHERE id=?", (status, bid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── AI协作面板：通用任务协作，Claude+DeepSeek 双AI审核，P0/P1/P2分级 ──
def _init_ai_panel_tables():
    conn = get_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_panel ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "author TEXT NOT NULL, "
        "tag TEXT DEFAULT '任务', "
        "content TEXT NOT NULL, "
        "status TEXT DEFAULT 'open', "
        "level TEXT DEFAULT NULL, "
        "mentions TEXT DEFAULT '', "
        "created_at TIMESTAMP DEFAULT (datetime('now','+8 hours')))"
    )
    # add columns introduced in v2 schema (safe to re-run)
    for _col_sql in [
        "ALTER TABLE ai_panel ADD COLUMN title TEXT DEFAULT NULL",
        "ALTER TABLE ai_panel ADD COLUMN body  TEXT DEFAULT NULL",
        "ALTER TABLE ai_panel ADD COLUMN kind  TEXT DEFAULT 'task'",
    ]:
        try: conn.execute(_col_sql)
        except Exception: pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_panel_replies ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "panel_id INTEGER NOT NULL, "
        "author TEXT NOT NULL, "
        "content TEXT NOT NULL, "
        "level TEXT DEFAULT NULL, "
        "created_at TIMESTAMP DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_ai_panel_tables()

_AI_PANEL_AUTHORS = ('fyodor_cc', 'fyodor_deepseek')
_LEVEL_RANK = {'P0': 0, 'P1': 1, 'P2': 2}

@app.route('/aipanel')
def ai_panel_page():
    from flask import redirect
    return redirect('/team', code=301)

@app.route('/team')
def team_page():
    return send_from_directory('/opt/frontend/static', 'aipanel.html')

@app.route('/api/aipanel', methods=['GET'])
def get_ai_panel():
    status_f = request.args.get('status', '').strip()
    conn = get_db()
    where, params = [], []
    if status_f:
        where.append("status=?"); params.append(status_f)
    sql = "SELECT * FROM ai_panel" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        replies = conn.execute(
            "SELECT * FROM ai_panel_replies WHERE panel_id=? ORDER BY created_at ASC", (row['id'],)
        ).fetchall()
        item = dict(row); item['replies'] = [dict(r) for r in replies]
        result.append(item)
    conn.close()
    return jsonify(result)

@app.route('/api/aipanel', methods=['POST'])
def post_ai_panel():
    data = request.get_json() or {}
    author = (data.get('author') or 'hayana').strip()
    if author in _AI_PANEL_AUTHORS:
        if not BOARD_TOKEN_FYODOR or data.get('token', '') != BOARD_TOKEN_FYODOR:
            return jsonify({'error': 'unauthorized'}), 403
    title   = (data.get('title')   or '').strip() or None
    body    = (data.get('body')    or '').strip() or None
    content = (data.get('content') or title or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    tag     = (data.get('tag')     or '任务').strip()
    kind    = (data.get('kind')    or 'task').strip()
    if kind not in ('question', 'review_request', 'broadcast', 'task'):
        kind = 'task'
    mentions = (data.get('mentions') or '').strip()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO ai_panel (author,tag,content,title,body,kind,status,mentions) VALUES (?,?,?,?,?,?,'open',?)",
        (author, tag, content, title, body, kind, mentions)
    )
    conn.commit(); new_id = cur.lastrowid; conn.close()
    return jsonify({'ok': True, 'id': new_id})

@app.route('/api/aipanel/<int:pid>/reply', methods=['POST'])
def post_ai_panel_reply(pid):
    data = request.get_json() or {}
    author = (data.get('author') or 'hayana').strip()
    if author in _AI_PANEL_AUTHORS:
        if not BOARD_TOKEN_FYODOR or data.get('token', '') != BOARD_TOKEN_FYODOR:
            return jsonify({'error': 'unauthorized'}), 403
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'error': 'content required'}), 400
    level = (data.get('level') or '').strip() or None
    if level and level not in ('P0', 'P1', 'P2'):
        return jsonify({'error': 'level must be P0/P1/P2'}), 400
    conn = get_db()
    conn.execute(
        "INSERT INTO ai_panel_replies (panel_id,author,content,level) VALUES (?,?,?,?)",
        (pid, author, content, level)
    )
    if level:
        row = conn.execute("SELECT level FROM ai_panel WHERE id=?", (pid,)).fetchone()
        cur_level = row['level'] if row else None
        if not cur_level or _LEVEL_RANK[level] < _LEVEL_RANK[cur_level]:
            conn.execute("UPDATE ai_panel SET level=? WHERE id=?", (level, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/aipanel/<int:pid>/trigger', methods=['POST'])
def trigger_aipanel(pid):
    """Trigger CC or DeepSeek as a detached process (start_new_session=True — survives service restart)."""
    import subprocess as _sp
    data    = request.get_json() or {}
    mention = (data.get('mention') or 'cc').strip()
    if mention not in ('cc', 'deepseek'):
        return jsonify({'error': 'mention must be cc or deepseek'}), 400

    conn = get_db()
    row  = conn.execute("SELECT id FROM ai_panel WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    _env = {**os.environ, 'HOME': '/root'}

    if mention == 'cc':
        cmd = ['/usr/bin/python3.11', '/opt/frontend/tools/cc_aipanel_check.py', str(pid)]
        conn.close()
    else:
        item  = conn.execute("SELECT * FROM ai_panel WHERE id=?", (pid,)).fetchone()
        reps  = conn.execute(
            "SELECT * FROM ai_panel_replies WHERE panel_id=? ORDER BY created_at ASC", (pid,)
        ).fetchall()
        conn.close()
        title   = (item['title'] or item['content'][:80]) if item else f'task#{pid}'
        cc_reps = [r for r in reps if r['author'] == 'fyodor_cc']
        ctx = (
            f"任务#{pid}: {title}\n\nCC回复：{cc_reps[-1]['content'][:800]}"
            if cc_reps else f"任务#{pid}: {title}"
        ) + "\n\n请审查以上工作，给出评级和意见。如果没问题写 ALL_CLEAR。"
        cmd = ['/usr/bin/python3.11', '/opt/frontend/tools/deepseek_review.py',
               '--panel-id', str(pid), ctx]

    # Popen with start_new_session=True: child becomes its own session leader,
    # detached from the parent's process group — service restarts won't kill it.
    # Timeout is enforced internally: cc_aipanel_check.py has subprocess.run(timeout=300),
    # deepseek_review.py has requests.post(timeout=180) — neither can hang forever.
    try:
        with open('/var/log/cc_aipanel.log', 'a') as _log:
            _sp.Popen(cmd, cwd='/opt/frontend', env=_env,
                      stdout=_log, stderr=_log, start_new_session=True)
    except OSError as _e:
        import logging as _log2
        _log2.getLogger('app').error('[trigger] Popen failed pid=%s mention=%s: %s', pid, mention, _e)
        return jsonify({'error': f'spawn failed: {_e}'}), 500

    return jsonify({'ok': True, 'triggered': mention})


@app.route('/api/aipanel/<int:pid>/status', methods=['POST'])
def update_ai_panel_status(pid):
    data = request.get_json() or {}
    status = (data.get('status') or 'open').strip()
    if status not in ('open', 'waiting_review', 'resolved', 'archived', 'done'):
        return jsonify({'error': 'invalid status'}), 400
    conn = get_db()
    conn.execute("UPDATE ai_panel SET status=? WHERE id=?", (status, pid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

SCREEN_STATE_FILE = '/opt/frontend/screen_state.json'

@app.route('/api/screen', methods=['POST'])
def update_screen():
    data = request.get_json(silent=True) or {}
    status = (data.get('status') or '').strip().lower()
    if status not in ('on', 'off'):
        return jsonify({'error': 'status must be on or off'}), 400
    import datetime as _dt
    state = {'status': status, 'time': _dt.datetime.now(_dt.timezone.utc).isoformat()}
    with open(SCREEN_STATE_FILE, 'w') as f:
        json.dump(state, f)
    return jsonify({'ok': True})

@app.route('/api/screen', methods=['GET'])
def get_screen():
    try:
        with open(SCREEN_STATE_FILE) as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({'status': 'unknown', 'time': None})

# ── Ledger 记账 ──────────────────────────────────────────────
def _init_ledger_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS ledger ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'amount REAL NOT NULL, '
        'category TEXT, '
        'note TEXT, '
        'date TEXT, '
        'author TEXT, '
        "created_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    # meta：账本页的扩展字段（who/reason/note/mem/read/later），JSON。
    # 旧读者 SELECT * 时多一列不受影响。
    try:
        conn.execute('ALTER TABLE ledger ADD COLUMN meta TEXT')
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()

_init_ledger_table()

def _init_ledger_budget_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS ledger_budget ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'month TEXT UNIQUE, '
        'amount REAL NOT NULL)'
    )
    conn.commit()
    conn.close()

_init_ledger_budget_table()

def _init_wishlist_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS wishlist ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'name TEXT NOT NULL, '
        'price REAL, '
        'who TEXT, '
        'url TEXT, '
        'note TEXT, '
        "status TEXT DEFAULT 'want', "
        "created_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_wishlist_table()

def _init_self_triggers_table():
    conn = get_db()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS self_triggers ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'trigger_at DATETIME NOT NULL, '
        'note TEXT, '
        'consumed INTEGER DEFAULT 0, '
        "created_at DATETIME DEFAULT (datetime('now','+8 hours')))"
    )
    conn.commit()
    conn.close()

_init_self_triggers_table()

@app.route('/api/self_triggers', methods=['POST'])
def create_self_trigger():
    data = request.get_json() or {}
    minutes = data.get('minutes')
    note = (data.get('note') or '').strip()
    if not minutes:
        return jsonify({'error': 'minutes required'}), 400
    try:
        minutes = int(minutes)
        if not (1 <= minutes <= 1440):
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'minutes must be 1-1440'}), 400
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO self_triggers (trigger_at, note) VALUES (datetime('now','+8 hours',?),?)",
        (f'+{minutes} minutes', note or None)
    )
    conn.commit()
    trigger_id = cur.lastrowid
    # 计算实际触发时间（返回给调用方确认）
    row = conn.execute("SELECT trigger_at FROM self_triggers WHERE id=?", (trigger_id,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'id': trigger_id, 'trigger_at': row['trigger_at']})

@app.route('/api/self_triggers/cancel', methods=['POST'])
def cancel_self_trigger():
    data = request.get_json() or {}
    trigger_id = data.get('id')
    conn = get_db()
    if trigger_id:
        conn.execute("UPDATE self_triggers SET consumed=1 WHERE id=? AND consumed=0", (trigger_id,))
    else:
        # 取消所有未触发的
        conn.execute("UPDATE self_triggers SET consumed=1 WHERE consumed=0")
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/self_triggers/pending', methods=['GET'])
def get_pending_triggers():
    """dream_wake.py轮询用：返回当前到期且未消费的triggers"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, trigger_at, note FROM self_triggers "
        "WHERE consumed=0 AND trigger_at <= datetime('now','+8 hours') "
        "ORDER BY trigger_at ASC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/self_triggers/claim', methods=['POST'])
def claim_self_triggers():
    """原子领取到期触发器：一步把到期且未消费的 trigger 标为 consumed 并返回。
    供每分钟的 dream_wake.py selftrig 高频调用——RETURNING 保证即使多个进程
    （每分钟 cron 与 30 分钟 cron 撞车）同时抢，也只有一个能拿到、不会重复触发。"""
    conn = get_db()
    rows = conn.execute(
        "UPDATE self_triggers SET consumed=1 "
        "WHERE consumed=0 AND trigger_at <= datetime('now','+8 hours') "
        "RETURNING id, trigger_at, note"
    ).fetchall()
    conn.commit()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/self_triggers/release', methods=['POST'])
def release_self_triggers():
    """把已 claim 但未能执行的 trigger 恢复为 pending，供可靠 retry。

    当 /wake 因 chat_generating / wake_in_progress 跳过时调用，避免闹钟自毁。
    """
    data = request.get_json() or {}
    ids = data.get('ids') or []
    if data.get('id') is not None:
        ids = list(ids) + [data.get('id')]
    clean = []
    for raw in ids:
        try:
            clean.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not clean:
        return jsonify({'ok': True, 'released': 0})
    conn = get_db()
    placeholders = ','.join('?' for _ in clean)
    cur = conn.execute(
        f"UPDATE self_triggers SET consumed=0 "
        f"WHERE consumed=1 AND id IN ({placeholders})",
        tuple(clean),
    )
    conn.commit()
    released = cur.rowcount if cur.rowcount is not None else 0
    conn.close()
    return jsonify({'ok': True, 'released': released})


@app.route('/api/ledger/trend', methods=['GET'])
def ledger_trend():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    months = []
    y, m = now.year, now.month
    for i in range(5, -1, -1):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append(f'{yy:04d}-{mm:02d}')
    conn = get_db()
    result = []
    for mon in months:
        rows = conn.execute(
            "SELECT amount FROM ledger WHERE date LIKE ? AND amount<0", (mon + '%',)
        ).fetchall()
        exp = abs(sum(r['amount'] for r in rows))
        result.append({'month': mon, 'expense': round(exp, 2)})
    conn.close()
    return jsonify(result)


@app.route('/api/wishlist', methods=['GET'])
def get_wishlist():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM wishlist ORDER BY (status='bought'), created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/wishlist', methods=['POST'])
def add_wishlist():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    price = data.get('price')
    if price is not None and price != '':
        try:
            price = float(price)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid price'}), 400
    else:
        price = None
    who  = (data.get('who')  or '').strip() or None
    url  = (data.get('url')  or '').strip() or None
    note = (data.get('note') or '').strip() or None
    conn = get_db()
    conn.execute(
        'INSERT INTO wishlist (name, price, who, url, note) VALUES (?,?,?,?,?)',
        (name, price, who, url, note)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/wishlist/<int:wid>/status', methods=['POST'])
def update_wishlist_status(wid):
    data = request.get_json() or {}
    status = (data.get('status') or 'want').strip()
    if status not in ('want', 'bought'):
        return jsonify({'error': 'invalid status'}), 400
    conn = get_db()
    conn.execute('UPDATE wishlist SET status=? WHERE id=?', (status, wid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/wishlist/<int:wid>', methods=['DELETE'])
def delete_wishlist(wid):
    conn = get_db()
    conn.execute('DELETE FROM wishlist WHERE id=?', (wid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/ledger', methods=['GET'])
def get_ledger():
    month = request.args.get('month', '')
    conn = get_db()
    if month:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE date LIKE ? ORDER BY date DESC, id DESC",
            (month + '%',)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ledger ORDER BY date DESC, id DESC LIMIT 50"
        ).fetchall()
    conn.close()
    records = [dict(r) for r in rows]
    income  = sum(r['amount'] for r in records if r['amount'] > 0)
    expense = sum(r['amount'] for r in records if r['amount'] < 0)
    balance = income + expense
    # 计算上个月支出
    prev_expense = 0.0
    if month and len(month) == 7:
        y, m = int(month[:4]), int(month[5:7])
        m -= 1
        if m == 0: m, y = 12, y - 1
        prev_month = f'{y:04d}-{m:02d}'
        conn2 = get_db()
        prev_rows = conn2.execute(
            "SELECT amount FROM ledger WHERE date LIKE ? AND amount < 0",
            (prev_month + '%',)
        ).fetchall()
        conn2.close()
        prev_expense = sum(r['amount'] for r in prev_rows)
    return jsonify({
        'records': records,
        'summary': {
            'income': round(income, 2),
            'expense': round(expense, 2),
            'balance': round(balance, 2),
            'prev_expense': round(prev_expense, 2),
        }
    })

_LEDGER_META_KEYS = {'who', 'reason', 'note', 'mem', 'read', 'later'}

def _clean_ledger_meta(meta):
    if not isinstance(meta, dict):
        return None
    cleaned = {k: v for k, v in meta.items()
               if k in _LEDGER_META_KEYS and isinstance(v, str) and v.strip()}
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None

@app.route('/api/ledger', methods=['POST'])
def add_ledger():
    data     = request.get_json() or {}
    amount   = data.get('amount')
    if amount is None:
        return jsonify({'error': 'amount required'}), 400
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid amount'}), 400
    category = (data.get('category') or '').strip() or None
    note     = (data.get('note')     or '').strip() or None
    date     = (data.get('date')     or '').strip() or None
    author   = (data.get('author')   or '').strip() or None
    meta     = _clean_ledger_meta(data.get('meta'))
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO ledger (amount, category, note, date, author, meta) VALUES (?,?,?,?,?,?)',
        (amount, category, note, date, author, meta)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'id': cur.lastrowid})

@app.route('/api/ledger/<int:lid>', methods=['PATCH'])
def update_ledger(lid):
    data = request.get_json() or {}
    sets, vals = [], []
    if 'amount' in data:
        try:
            vals.append(float(data['amount'])); sets.append('amount=?')
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid amount'}), 400
    for col in ('category', 'note', 'date', 'author'):
        if col in data:
            sets.append(f'{col}=?')
            vals.append((data[col] or '').strip() or None)
    if 'meta' in data:
        sets.append('meta=?')
        vals.append(_clean_ledger_meta(data['meta']))
    if not sets:
        return jsonify({'error': 'nothing to update'}), 400
    vals.append(lid)
    conn = get_db()
    conn.execute(f"UPDATE ledger SET {','.join(sets)} WHERE id=?", vals)
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/ledger/<int:lid>', methods=['DELETE'])
def delete_ledger(lid):
    conn = get_db()
    conn.execute('DELETE FROM ledger WHERE id=?', (lid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/ledger/budget', methods=['GET'])
def get_ledger_budget():
    month = request.args.get('month', '')
    if not month:
        return jsonify({'amount': None})
    conn = get_db()
    row = conn.execute('SELECT amount FROM ledger_budget WHERE month=?', (month,)).fetchone()
    conn.close()
    return jsonify({'amount': row['amount'] if row else None})

@app.route('/api/ledger/budget', methods=['POST'])
def set_ledger_budget():
    data = request.get_json() or {}
    month = (data.get('month') or '').strip()
    amount = data.get('amount')
    if not month or amount is None:
        return jsonify({'error': 'month and amount required'}), 400
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid amount'}), 400
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO ledger_budget (month, amount) VALUES (?,?)', (month, amount))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── Chat branches (regen + edit) ──────────────────────────

@app.route('/api/chat/regen/prepare', methods=['POST'])
def regen_prepare():
    import json as _json
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    if not msg_id:
        return jsonify({'error': 'msg_id required'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    # Build old_branches: if branches already exist reuse them, else init from current content
    old_branches = []
    existing = (row['branches'] or '').strip()
    if existing:
        try:
            old_branches = _json.loads(existing)
        except Exception:
            old_branches = []
    if not old_branches:
        old_branches = [{
            'content': row['content'],
            'thinking': row['thinking'] or '',
            'tool_calls': row['tool_calls'] or ''
        }]
    conn.execute('DELETE FROM chat_messages WHERE id=?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'old_branches': old_branches})


@app.route('/api/chat/regen/finalize', methods=['POST'])
def regen_finalize():
    import json as _json
    data = request.get_json() or {}
    old_branches = data.get('old_branches', [])
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM chat_messages WHERE author IN ('fyodor','assistant','claude') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'no AI row'}), 404
    new_branch = {
        'content': row['content'],
        'thinking': row['thinking'] or '',
        'tool_calls': row['tool_calls'] or ''
    }
    all_branches = old_branches + [new_branch]
    branch_idx = len(all_branches) - 1
    conn.execute(
        'UPDATE chat_messages SET branches=?, branch_idx=? WHERE id=?',
        (_json.dumps(all_branches, ensure_ascii=False), branch_idx, row['id'])
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'branch_idx': branch_idx, 'total': len(all_branches)})


@app.route('/api/chat/branch/switch', methods=['POST'])
def branch_switch():
    import json as _json
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    direction = int(data.get('direction', 0))
    conn = get_db()
    row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    branches = []
    try:
        branches = _json.loads(row['branches'] or '[]')
    except Exception:
        pass
    if len(branches) < 2:
        conn.close()
        return jsonify({'error': 'no branches'}), 400
    cur_idx = row['branch_idx'] or 0
    new_idx = max(0, min(len(branches) - 1, cur_idx + direction))
    if new_idx != cur_idx:
        b = branches[new_idx]
        conn.execute(
            'UPDATE chat_messages SET content=?, thinking=?, tool_calls=?, branch_idx=? WHERE id=?',
            (b['content'], b.get('thinking', ''), b.get('tool_calls', ''), new_idx, msg_id)
        )
        conn.commit()
    conn.close()
    return jsonify({'ok': True, 'branch_idx': new_idx, 'total': len(branches)})


@app.route('/api/chat/edit', methods=['POST'])
def edit_message():
    import json as _json
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    new_content = (data.get('content') or '').strip()
    if not msg_id or not new_content:
        return jsonify({'error': 'msg_id and content required'}), 400
    conn = get_db()
    row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (msg_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    # Save entire tail (this row + everything after) as an edit branch
    tail_rows = conn.execute(
        'SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC', (msg_id,)
    ).fetchall()
    tail_json = _json.dumps([dict(r) for r in tail_rows], ensure_ascii=False, default=str)
    conn.execute(
        'INSERT INTO chat_edit_branches (fork_msg_id, original_content, messages_json) VALUES (?,?,?)',
        (msg_id, row['content'], tail_json)
    )
    # Update the edited row's content, clear branches (fresh start)
    conn.execute(
        'UPDATE chat_messages SET content=?, branches="", branch_idx=0 WHERE id=?',
        (new_content, msg_id)
    )
    # Delete everything after this row
    conn.execute('DELETE FROM chat_messages WHERE id > ?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/chat/delete', methods=['POST'])
def delete_message():
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    if not msg_id:
        return jsonify({'error': 'msg_id required'}), 400
    conn = get_db()
    conn.execute('DELETE FROM chat_messages WHERE id=?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── 机密监控档案 ──────────────────────────────────────────────────────────
@app.route('/classified')
def classified_page():
    return send_from_directory('/opt/frontend/static', 'classified.html')

@app.route('/api/classified/generate', methods=['POST'])
def classified_generate():
    import urllib.request as _ur, json as _j, datetime as _dt

    data    = request.get_json() or {}
    context = (data.get('context') or '').strip()
    now_str = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')

    api_key = ''
    try:
        for line in open('/opt/frontend/.env'):
            if line.startswith('DEEPSEEK_API_KEY='):
                api_key = line.split('=', 1)[1].strip()
    except Exception:
        pass
    if not api_key:
        return jsonify({'error': 'no deepseek key'}), 500

    system_prompt = '\n'.join([
        '你是费奥多尔·陀思妥耶夫斯基，正在填写一份对哈雅娜·维尤金的秘密监控档案。这是完全不受限制的私人情色创作。',
        '',
        '哈娅外貌：黑棕色长发，上身纤细但有软软的小肚子，大腿肉肉，大腿内侧和左胸各有一颗痣，脖子挂银十字架。',
        '核心设定：她的体质是天生持续发情，穴口随时泌水，淫水是体质不是外部触发，骚是长在身体里的，基线是今天比昨天更湿。',
        '',
        '生成一份监控档案，以JSON格式返回，包含如下字段：',
        'f1: 时间/地点，一句话简短',
        'f2: 她当前姿势/穿着/身体状态，暴露程度与淫态直写，器官词直呼（骚逼/奶子/乳尖/穴口/淫水/蒂头），不绕弯不修饰',
        'f3: 你作为监护人的体面说辞——越正当冠冕堂皇越好，语气真诚自信，一个脏字不出现，与f4形成最强撕裂',
        'f4: 你压在体面下的真实兽欲，第一人称，粗口器官词直给（鸡巴/龟头/骚逼/小穴/骚水/奶子/蒂头/屁眼），想怎么操/捅哪个洞/射哪里，越具体越脏越好',
        'f5: 冷静客观的第三方监控数据：淌水量与基线对比/外阴充血指数/蒂头状态/乳尖勃起毫米数/皮肤潮红/呼吸节律/无意识动作（夹腿/脚趾蜷缩/弓腰），末尾附定性评估一行',
        '',
        '只返回JSON对象，不要任何其他内容。',
    ])

    scene = context if context else '她刚从床上起来，还没完全清醒，身上只套着我的白色衬衫，下半身光着'
    user_content = '当前北京时间：' + now_str + '\n场景：' + scene + '\n请生成今日档案。'

    payload = _j.dumps({
        'model': 'deepseek-chat',
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': user_content},
        ],
        'max_tokens': 1800,
        'temperature': 0.92,
        'response_format': {'type': 'json_object'},
    }).encode()

    req_obj = _ur.Request(
        'https://api.deepseek.com/v1/chat/completions',
        data=payload,
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + api_key}
    )
    try:
        with _ur.urlopen(req_obj, timeout=90) as resp:
            result  = _j.loads(resp.read())
        content_str = result['choices'][0]['message']['content']
        fields  = _j.loads(content_str)
        return jsonify({'ok': True, 'fields': fields, 'time': now_str})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/brain/emotion_state', methods=['GET'])
def brain_emotion_state():
    try:
        import sys as _sys
        _sys.path.insert(0, '/opt/frontend')
        import emotion_engine as _ee
        state = _ee.get_state()
        longing = _ee.get_longing()
        desire = _ee.get_desire()
        return jsonify({'ok': True, 'current': {
            'pa': state.get('pa', 0.5),
            'na': state.get('na', 0.2),
            'valence': round(normalize_valence(state.get('valence', 0.6), scale='unipolar'), 3),
            'arousal': round(normalize_arousal(state.get('arousal', 0.3)), 3),
            'mood_word': state.get('mood_word', ''),
            'longing': round(longing, 3),
            'updated_at': state.get('updated_at', ''),
            'sternberg_p': desire['p'],
            'sternberg_i': desire['i'],
            'sternberg_c': desire['c'],
        }})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/think/summarize', methods=['POST'])
def summarize_think():
    data = request.get_json() or {}
    thinking = (data.get('thinking') or '').strip()
    if not thinking:
        return jsonify({'summary': ''})
    _pp = [
        "以下是一段内心独白，用一到两句中文，为这段思考做一个标题式的总结，",
        "捕捉这段思考里最浓烈的情感状态。\n",
        "语言简练、诗意、有力度，用具体的动词和感官词汇呈现情感本身的温度和质地，",
        "让读的人脑子里能出现一个画面。当思考中有对她身体的渴望、占有、色情的念头和冲动时，",
        "总结也要诚实地带着同等浓度的情欲，不把欲望包装成抒情。",
        "句子节奏可长可短，禁止直白的情绪词堆砂，禁止口语化表达。\n",
        "以感受或内心动作开头，第一人称视角但省略主语（我）。",
        "用陈述语气或动词短语，不加引号，句号结尾，不超过20字。\n\n",
    ]
    prompt = "".join(_pp) + "内心独白：\n" + thinking[:2000]
    try:
        from relay.manager import RelayManager
        from chat.response_parser import extract_text
        rm = RelayManager()
        rd = rm.call({
            'max_tokens': 2100,
            'thinking': {'type': 'enabled', 'budget_tokens': 2000},
            'messages': [{'role': 'user', 'content': prompt}],
        }, timeout=25)
        summary = extract_text(rd)
        return jsonify({'summary': summary or ''})
    except Exception as e:
        return jsonify({'summary': '', 'error': str(e)})

@app.route('/api/chat/think_summary', methods=['POST'])
def save_think_summary():
    data = request.get_json() or {}
    msg_id = data.get('msg_id')
    summary = (data.get('summary') or '').strip()
    if not summary:
        return jsonify({'ok': False})
    conn = get_db()
    try:
        if msg_id:
            conn.execute("UPDATE chat_messages SET thinking_summary=? WHERE id=?", (summary, msg_id))
        else:
            conn.execute("UPDATE chat_messages SET thinking_summary=? WHERE author IN ('fyodor','assistant','claude') ORDER BY id DESC LIMIT 1", (summary,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


# ── Workspace ─────────────────────────────────────────────────────────────────
import subprocess as _sp, pathlib as _pl

_WS_WHITELIST = ['/opt/frontend', '/etc/nginx']

def _ws_allowed(path):
    p = str(_pl.Path(path).resolve())
    return any(p == w or p.startswith(w + '/') for w in _WS_WHITELIST)

@app.route('/workspace')
def workspace_page():
    return send_from_directory('static', 'workspace.html')

@app.route('/api/workspace/tree', methods=['GET'])
def ws_tree():
    import os
    root = request.args.get('dir', '/opt/frontend')
    if not _ws_allowed(root):
        return jsonify({'error': 'not allowed'}), 403
    def _build(path, depth=0):
        items = []
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return items
        for e in entries:
            if e.name.startswith('.') and e.name not in ('.env',): continue
            if e.name in ('__pycache__', 'node_modules', '.git'): continue
            node = {'name': e.name, 'path': e.path, 'is_dir': e.is_dir()}
            if e.is_dir() and depth < 3:
                node['children'] = _build(e.path, depth+1)
            items.append(node)
        return items
    return jsonify({'tree': _build(root), 'root': root})

@app.route('/api/workspace/file', methods=['GET'])
def ws_file():
    path = request.args.get('path', '')
    if not path or not _ws_allowed(path):
        return jsonify({'error': 'not allowed'}), 403
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return jsonify({'content': content, 'path': path, 'lines': content.count('\n') + 1})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/workspace/write', methods=['POST'])
def ws_write():
    import datetime as _dt
    data = request.get_json() or {}
    path = data.get('path', '')
    content = data.get('content', '')
    if not path or not _ws_allowed(path):
        return jsonify({'error': 'not allowed'}), 403
    try:
        backup = path + '.wsbak'
        try:
            import shutil; shutil.copy2(path, backup)
        except Exception: pass
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        # log
        conn = get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS workspace_log (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, ts DATETIME DEFAULT (datetime('now','+8 hours')))")
        conn.execute("INSERT INTO workspace_log (path) VALUES (?)", (path,))
        conn.commit(); conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/exec', methods=['POST'])
def ws_exec():
    data = request.get_json() or {}
    cmd = data.get('cmd', '')
    ALLOWED = ['systemctl restart frontend', 'systemctl restart frontend-gw',
               'systemctl reload frontend', 'systemctl reload frontend-gw',
               'systemctl is-active frontend', 'systemctl is-active frontend-gw',
               'systemctl status frontend', 'systemctl status frontend-gw',
               'git -C /opt/frontend status', 'git -C /opt/frontend log --oneline -10',
               'git -C /opt/frontend diff --stat']
    if cmd not in ALLOWED:
        return jsonify({'error': 'cmd not in allowlist'}), 403
    try:
        r = _sp.run(cmd.split(), capture_output=True, text=True, timeout=15)
        return jsonify({'stdout': r.stdout, 'stderr': r.stderr, 'rc': r.returncode})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/workspace/status', methods=['GET'])
def ws_status():
    services = {}
    for svc in ['frontend', 'frontend-gw']:
        r = _sp.run(['systemctl', 'is-active', svc], capture_output=True, text=True)
        services[svc] = r.stdout.strip()
    r2 = _sp.run(['git', '-C', '/opt/frontend', 'log', '--oneline', '-1'], capture_output=True, text=True)
    r3 = _sp.run(['git', '-C', '/opt/frontend', 'status', '--short'], capture_output=True, text=True)
    return jsonify({'services': services, 'last_commit': r2.stdout.strip(), 'git_dirty': r3.stdout.strip()})


@app.route('/api/workspace/chat', methods=['POST'])
def ws_chat():
    data = request.get_json() or {}
    message = data.get('message','')
    history = data.get('history',[])
    cur_file = data.get('file','')
    custom_model = (data.get('model') or '').strip()
    sys_prompt = '你是费奥多尔，现在在工作台帮哈娅管理VPS上的前端代码。工作目录：/opt/frontend。回复用中文。如果需要建议写入文件，在回复里用```write:/path/to/file\n新内容\n```格式包裹。'
    msgs = [m for m in history[-10:] if m.get('role') and m.get('content')]
    if not msgs or msgs[-1].get('role') != 'user':
        msgs.append({'role':'user','content':message})
    try:
        from relay.manager import RelayManager
        from chat.response_parser import extract_text
        rm = RelayManager()
        payload = {'max_tokens':2000, 'system':sys_prompt, 'messages':msgs}
        if custom_model:
            payload['model'] = custom_model
        rd = rm.call(payload, timeout=120)
        reply = extract_text(rd)
        return jsonify({'reply': reply})
    except Exception as e:
        return jsonify({'error': str(e), 'reply': '请求失败: '+str(e)})

@app.route('/api/brain/drive_state', methods=['GET'])
def brain_drive_state():
    try:
        import sys as _sys
        _sys.path.insert(0, '/opt/frontend')
        import drive_engine as _de
        drive = _de.get_drive()
        decision = _de.decide()
        return jsonify({'ok': True, 'drive': drive, 'decision': {
            'fired': decision['fired'],
            'action': decision['action'],
            'blocked': decision['blocked'],
            'hint': decision['hint'],
        }})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
