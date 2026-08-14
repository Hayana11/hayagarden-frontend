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
from chat.attachment_contract import (
    ALLOWED_TEXT_FILE_EXTENSIONS,
    MAX_TEXT_FILE_BYTES,
    AttachmentValidationError,
    read_limited_upload,
    render_markdown_preview_page,
    reencode_chat_image,
    resolve_uploaded_file_url,
    sandbox_preview_shell,
    safe_child_path,
    validate_uploaded_file_reference,
    write_limited_text_upload,
)

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
# API_URL/API_KEY/MODEL ä¸å†æ˜¯è¿™é‡Œçš„å†»ç»“å¸¸é‡ï¼šè°è¦å‘è¯·æ±‚ï¼Œ
# å°± new ä¸€ä¸ª relay.manager.RelayManager()ï¼Œæ°¸è¿œæ‹¿å®æ—¶å€¼ã€‚

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# â”€â”€ æ–‡ä»¶æ”¶å‘ï¼šchat_messages æ–°å¢ file_url/file_name/choices ä¸‰åˆ— â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# å»¶ç»­â€œå“ªä¸ªå­—æ®µæœ‰å€¼å°±æ˜¯å“ªç§æ°”æ³¡â€çš„è€è§„çŸ©ï¼ˆimage_url æœ‰å€¼=å›¾ç‰‡ï¼‰ï¼š
#   file_url æœ‰å€¼=æ–‡ä»¶å¡ç‰‡ï¼Œchoices æœ‰å€¼(JSON æ•°ç»„)=é€‰æ‹©å™¨æŒ‰é’®ç»„ã€‚
# å¹‚ç­‰ migrationï¼Œè·‘å‡ æ¬¡éƒ½å®‰å…¨ã€‚
FILES_DIR = '/opt/frontend/static/uploads/files'
ALLOWED_FILE_EXT = set(ALLOWED_TEXT_FILE_EXTENSIONS)
MAX_FILE_BYTES = MAX_TEXT_FILE_BYTES

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
    from chat.daily_schema import ensure_chat_messages_source_kind_logged
    ensure_chat_messages_source_kind_logged(DB_PATH, connect_fn=lambda p: __import__('sqlite3').connect(p))


_migrate_chat_columns()
group_chat_store.ensure_schema(DB_PATH)
context_usage_store.ensure_schema(DB_PATH)
moments_store.ensure_schema(DB_PATH, gallery_store.DB_PATH)
from chat.rewrite_staging import ensure_schema_for_path as _rewrite_staging_ensure_schema
_rewrite_staging_ensure_schema(DB_PATH)
# Legacy edit archives; create if missing (production already has it).
try:
    _eb = get_db()
    _eb.execute(
        'CREATE TABLE IF NOT EXISTS chat_edit_branches ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'fork_msg_id INTEGER, original_content TEXT, messages_json TEXT)'
    )
    _eb.commit()
    _eb.close()
except Exception:
    pass
from chat.daily_context import ensure_schema_logged as _daily_context_ensure_schema
_daily_context_ensure_schema(DB_PATH)
from wake.concern_resolution import ensure_concern_closure_schema_for_path
ensure_concern_closure_schema_for_path(DB_PATH)
app.register_blueprint(create_context_usage_blueprint(
    db_path=DB_PATH,
    report_token_getter=lambda: CONTEXT_USAGE_REPORT_TOKEN,
))
from daily_context_routes import create_daily_context_blueprint
app.register_blueprint(create_daily_context_blueprint(db_path=DB_PATH))
from context_window_routes import create_context_window_blueprint
app.register_blueprint(create_context_window_blueprint(db_path=DB_PATH))
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


# â”€â”€ Artifactï¼ˆè´¹ä½³ç”Ÿæˆçš„ HTML/Markdown/Word äº§ç‰©ï¼‰â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    meta, content = artifact_store.read_content(aid)
    if not meta or content is None:
        return jsonify({'error': 'not found'}), 404
    if meta['type'] in {'html', 'markdown'}:
        return _sandbox_preview_shell('/api/artifacts/%d/content' % aid)
    return jsonify({'error': 'docx ä¸æ”¯æŒåœ¨çº¿é¢„è§ˆï¼Œç›´æ¥ä¸‹è½½æŸ¥çœ‹',
                     'download_url': '/api/artifacts/%d/download' % aid}), 400


@app.route('/api/artifacts/<int:aid>/content', methods=['GET'])
def artifact_html_content(aid):
    import artifact_store
    meta, content = artifact_store.read_content(aid)
    if not meta or content is None:
        return jsonify({'error': 'not found'}), 404
    if meta['type'] == 'html':
        preview_content = content.decode('utf-8', errors='replace')
    elif meta['type'] == 'markdown':
        preview_content = render_markdown_preview_page(
            meta['title'],
            content.decode('utf-8', errors='replace'),
        )
    else:
        return jsonify({'error': 'HTML content only'}), 400
    response = jsonify({'content': preview_content})
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def _sandbox_preview_shell(content_url):
    """Same-origin shell; untrusted HTML runs only in an opaque sandbox origin."""
    response = Response(sandbox_preview_shell(content_url), mimetype='text/html')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response

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
@app.route('/dash/')
def dash():
    # Prefer the new React build when deployed; fallback to legacy static dash.
    # Explicit /dash/ (in addition to /dash) â€” do not rely on app-wide strict_slashes.
    if os.path.exists(os.path.join(APP_DIST_DIR, 'index.html')):
        resp = send_from_directory(APP_DIST_DIR, 'index.html')
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        return resp
    return send_from_directory('/opt/frontend/static', 'dash.html')

@app.route('/dash/<path:subpath>')
def dash_subpath(subpath):
    """SPA deep-link fallback for React BrowserRouter under /dash.

    Real files under app/dist (JS/CSS/assets) are served as-is.
    Any other /dash/* path returns index.html so client routing can resolve
    /dash/contacts, /dash/chat, /dash/settings, etc.

    Does not register under /api, /read, /board, or /static.
    """
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
@app.route('/calendar.html')
@app.route('/static/calendar.html')
def calendar():
    from flask import redirect
    return redirect('/dash', code=302)

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
    # é˜²æ­¢æŠŠæœªè¾“å‡ºå®Œçš„<thinking>åŸå§‹å—å½“æˆæ­£æ–‡å­˜è¿›æ¥ï¼ˆè¾“å‡ºè¢«æˆªæ–­æ—¶å¸¸è§ï¼‰
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


@app.route('/api/weather/now', methods=['GET'])
def get_weather_now():
    """Shared weather authority for Dash (and observability). Fail-closed â€” never mock."""
    from chat.weather_authority import try_fetch_weather_now
    snap = try_fetch_weather_now()
    if snap is None:
        return jsonify({
            'ok': False,
            'unavailable': True,
            'location': 'å‰æ—å¸‚',
            'source': 'open-meteo',
        }), 200
    return jsonify(snap.as_api_dict())


@app.route('/api/countdowns', methods=['POST'])
def add_countdown():
    data = request.get_json()
    conn = get_db()
    conn.execute("INSERT INTO countdowns (title,target_date,emoji,type) VALUES (?,?,?,?)",
        (data['title'], data['target_date']ç¾ºöÚ$z{-®éÜj×W†6WBW†6WF–öã Ğ¢G'“ Ğ¢6öæâç&öÆÆ&6²‚Ğ¢W†6WBW†6WF–öã Ğ¢70Ğ¢6öæâæ6Æ÷6R‚Ğ¢&—6PĞ¢6öæâæ6Æ÷6R‚Ğ¢7Fv–ærÒ&W7VÇBævWB‚w7Fv–ærr’÷"7Fv–æuöf÷%ö6ÆVçW÷"·ĞĞ¢ÖöFRÒ&W7VÇBævWB‚vf–æÆ—¦UöÖöFRr’÷"v7F—fFRpĞ Ğ¢FVböVF—Eö7F—fFUö†öö·2‚“ Ğ¢G'“ Ğ¢g&öÒ6†Bæ–çFW&7F–öå÷7FFR–×÷'BF÷V6…÷W6W%ö–çFW&7F–öàĞ¢F÷V6…÷W6W%ö–çFW&7F–öâ†vWEöF"Ğ¢W†6WBW†6WF–öã Ğ¢70Ğ¢G'“ Ğ¢–×÷'B–çFW&æÅ÷7FFU÷6†F÷r2÷6†F÷pĞ¢–b÷6†F÷ræ—5÷W6W%öWfVçG5öVæ&ÆVB‚“ Ğ¢÷6†F÷ræG&–å÷6†F÷uö÷WF&÷…ö&W7EöVff÷'B†F%÷FƒÔD%õD‚Ğ¢W†6WBW†6WF–öã Ğ¢70Ğ Ğ¢–ÆöBÒö6ö×ÆWFU÷&Ww&—FUöf–æÆ—¦R€Ğ¢'uöÖöCÕ÷'rÀĞ¢&W7VÇC×&W7VÇBÀĞ¢7Fv–æs×7Fv–ærÀĞ¢ÖöFSÖÖöFRÀĞ¢–çfÆ–FFU÷&V6öãÒvVF—BrÀĞ¢66÷&UöÖW76vUö–C×&W7VÇBævWB‚vÖW76vUö–Br’ÀĞ¢66÷&U÷FW‡C×&W7VÇBævWB‚v6æF–FFUö6öçFVçBr’÷"rrÀĞ¢7F—fFUö†öö·3ÕöVF—Eö7F—fFUö†öö·2ÀĞ¢Ğ¢&WGW&â§6öæ–g’‡–ÆöBĞ Ğ Ğ¤ç&÷WFR‚rö’ö6†BöFVÆWFRrÂÖWF†öG3Õ²uõ5BuÒĞ¤6W&–Æ—¦Uö†—7F÷'•÷&Ww&—FPĞ¦FVbFVÆWFUöÖW76vR‚“ Ğ¢FFÒ&WVW7BævWEö§6öâ‚’÷"·ĞĞ¢×6uö–BÒFFævWB‚v×6uö–BrĞ¢–bæ÷B×6uö–C Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢v×6uö–B&WV—&VBwÒ’ÂC Ğ¢6öæâÒvWEöF"‚Ğ¢7W"Ò6öæâæW†V7WFR‚tDTÄUDRe$ôÒ6†EöÖW76vW2t„U$R–CÓòrÂ†×6uö–BÂ’Ğ¢FVÆWFVBÒ–çB†7W"ç&÷v6÷VçB÷"’â Ğ¢6öæâæ6öÖÖ—B‚Ğ¢6öæâæ6Æ÷6R‚Ğ¢–bFVÆWFVC Ğ¢–çfÆ–FFUö65÷&W6–FVçEöf÷%ö†—7F÷'•÷&Ww&—FR‚vFVÆWFRrĞ¢&WGW&â§6öæ–g’‡²vö²s¢G'VWÒĞ Ğ Ğ¢2)H)HiË®Zøny¹hê~j>j‚)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H Ğ¤ç&÷WFR‚rö6Æ76–f–VBrĞ¦FVb6Æ76–f–VE÷vR‚“ Ğ¢&WGW&â6VæEög&öÕöF—&V7F÷'’‚rö÷Bög&öçFVæB÷7FF–2rÂv6Æ76–f–VBæ‡FÖÂrĞ Ğ¤ç&÷WFR‚rö’ö6Æ76–f–VBövVæW&FRrÂÖWF†öG3Õ²uõ5BuÒĞ¦FVb6Æ76–f–VEövVæW&FR‚“ Ğ¢–×÷'BW&ÆÆ–"ç&WVW7B2÷W"Â§6öâ2ö¢ÂFFWF–ÖR2öG@Ğ Ğ¢FFÒ&WVW7BævWEö§6öâ‚’÷"·ĞĞ¢6öçFW‡BÒ†FFævWB‚v6öçFW‡Br’÷"rr’ç7G&—‚Ğ¢æ÷u÷7G"Ò…öGBæFFWF–ÖRçWF6æ÷r‚’²öGBçF–ÖVFVÇF††÷W'3Ó‚’’ç7G&gF–ÖR‚rU’ÒVÒÒVBTƒ¢TÒrĞ Ğ¢•ö¶W’ÒrpĞ¢G'“ Ğ¢f÷"Æ–æR–â÷Vâ‚rö÷Bög&öçFVæBòæVçbr“ Ğ¢–bÆ–æRç7F'G7v—F‚‚tDTU4TTµô•ô´U“Òr“ Ğ¢•ö¶W’ÒÆ–æRç7Æ—B‚sÒrÂ•³Òç7G&—‚Ğ¢W†6WBW†6WF–öã Ğ¢70Ğ¢–bæ÷B•ö¶W“ Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢væòFVW6VV²¶W’wÒ’ÂS Ğ Ğ¢7—7FVÕ÷&ö×BÒuÆâræ¦ö–â…°Ğ¢~KÚiŠş‹KZZ^ZI®[	L+~™˜h	ŞZj^ˆnZJ¾ijşYû®ûÈÎjÚ>YÊZ¾XiKˆK»ŞZûY8™¸^Z‰Ì+~{»N[
N˜yy¨NzyZøny¹hê~j>j8.‹ùiŠşZèÎXZKˆŞXù~™™X‹ny¨NzxK«®h8^ˆ›.X‰¾KÙÎ8"rÀĞ¢rrÀĞ¢~Y8Zˆ^ZIn‹(ÎûÉ®›¹j9^ˆ›.™[şXùûÈÎKˆ®‹ª¾{ªN{¸nKØniÈ‹Úş‹Úşy¨N[şˆ)®ZÙûÈÎZJ~ˆ[şˆ(ˆ(ûÈÎZJ~ˆ[şXh^Kê~Y(Î[znˆ;YNiÈKˆš)~yz>ûÈÎˆInZÙhÈ.™;nXØZÙ~iën8"rÀĞ¢~j[ø>ŠëîZé®ûÉ®Z[y¨NKÙ>‹JiŠşZJyIşhÈ{ºŞXùh8^ûÈÎz›NXú>™¨şi{nk8ÎkNûÈÎkz¾kNiŠşKÙ>‹JKˆŞiŠşZIn˜:ŠznXùûÈÎš©®iŠş™[şYÊ‹ª¾KÙ>˜xÎy¨NûÈÎYû®{«şiŠşK¸®ZJjùNiŠZJi»Nk›ş8"rÀĞ¢rrÀĞ¢~yIşh‰KˆK»Şy¹hê~j>jûÈÎKºT¥4ôîjÎ[Èş‹ùNY¹îûÈÎXÈ^Y
¾Zh.Kˆ¾ZÙ~jë^ûÉ¢rÀĞ¢vc¢i{n™{BşYËx+ûÈÎKˆXú^ŠùŞzèyúÒrÀĞ¢vc#¢Z[[Ù>X˜ŞZ{şX«òşz›şyØş‹ª¾KÙ>x«nhûÈÎi«N™Ë.zˆ¾[ªnKˆîkz¾hy»NXiûÈÎYšZéŠøŞy»NYÎûÈš©®˜ÂşZ[nZÙşK›>[	bşz›NXú2şkz¾kBş‰(.ZKNûÈûÈÎKˆŞ{¹^[ÊşKˆŞKúîš[rÀĞ¢vc3¢KÚKÙÎK‹®y¹hªNK«®y¨NKÙ>™Ú.ŠûN‹éî(	N(	N‹h®jÚ>[Ù>XjXi^Z.y¨~‹h®Z[ŞûÈÎŠúŞk	NyÉşŠù®ˆz®KúûÈÎKˆKŠ®ˆHşZÙ~KˆŞX{®xëûÈÎKˆæcN[Ú.h‰iÈ[Ë®i)^Š8"rÀĞ¢vcC¢KÚXè¾YÊKÙ>™Ú.Kˆ¾y¨NyÉşZéîX[ŞjË.ûÈÎzÊÎKˆK«®z{ûÈÎ{)~Xú>YšZéŠøŞy»N{¹ûÈ›Š[{Bş›éşZKBşš©®˜Âş[şz›Bşš©®kBşZ[nZÙş‰(.ZKBş[yËÎûÈûÈÎh;>hîK˜i8ÒşhØ^Y:®KŠ®kIâş[NY:®˜xÎûÈÎ‹h®X[~KÙ>‹h®ˆHş‹h®Z[ÒrÀĞ¢vcS¢Xk~™ÙZê.Šx.y¨NzÊÎKˆiky¹hê~i[hÚîûÉ®kxÎkN˜xşKˆîYû®{«şZûjùBşZIn™‹NXX^ŠhÈ~i[ş‰(.ZKNx«nhşK›>[	nX¸>‹[~jú¾{>i[şyªîˆ*NkÚî{ª"şYÎYˆ¨.[è²şizhHşŠønXªKÙÎûÈZKˆ[òşˆI®‹kî‰Ë~{Ê’ş[É>ˆ[ûÈûÈÎiÊ¾[î™˜NZé®h
~ŠøNKËKˆŠÂrÀĞ¢rrÀĞ¢~Xú®‹ùNY¹ä¥4ôîZû‹ûÈÎKˆŞŠhK»¾KÙ^X[nK¹nXh^Zë8"rÀĞ¢ÒĞ Ğ¢66VæRÒ6öçFW‡B–b6öçFW‡BVÇ6R~Z[X‰®K¸î[¨®Kˆ®‹[~iÚ^ûÈÎ‹ùk*ZèÎXZkˆ^˜i.ûÈÎ‹ª¾Kˆ®Xú®ZY~yØh‰y¨Ny›Şˆ›.ŠÎŠ¾ûÈÎKˆ¾XØ®‹ª¾XXyØpĞ¢W6W%ö6öçFVçBÒ~[Ù>X˜ŞXÉ~KªÎi{n™{NûÉ¢r²æ÷u÷7G"²uÆîYË®išşûÉ¢r²66VæR²uÆîŠû~yIşh‰K¸®iz^j>j8"pĞ Ğ¢–ÆöBÒö¢æGV×2‡°Ğ¢vÖöFVÂs¢vFVW6VV²Ö6†BrÀĞ¢vÖW76vW2s¢°Ğ¢²w&öÆRs¢w7—7FVÒrÂv6öçFVçBs¢7—7FVÕ÷&ö×GÒÀĞ¢²w&öÆRs¢wW6W"rÂv6öçFVçBs¢W6W%ö6öçFVçGÒÀĞ¢ÒÀĞ¢vÖ…÷Fö¶Vç2s¢ƒÀĞ¢wFV×W&GW&Rs¢ã“"ÀĞ¢w&W7öç6Uöf÷&ÖBs¢²wG—Rs¢v§6öåöö&¦V7BwÒÀĞ¢Ò’æVæ6öFR‚Ğ Ğ¢&Wöö&¢Ò÷W"å&WVW7B€Ğ¢v‡GG3¢òö’æFVW6VV²æ6öÒ÷cö6†Bö6ö×ÆWF–öç2rÀĞ¢FF×–ÆöBÀĞ¢†VFW'3×²t6öçFVçBÕG—Rs¢vÆ–6F–öâö§6öârÀĞ¢tWF†÷&—¦F–öâs¢t&V&W"r²•ö¶W—ĞĞ¢Ğ¢G'“ Ğ¢v—F‚÷W"çW&Æ÷Vâ‡&Wöö&¢ÂF–ÖV÷WCÓ“’2&W7 Ğ¢&W7VÇBÒö¢æÆöG2‡&W7ç&VB‚’Ğ¢6öçFVçE÷7G"Ò&W7VÇE²v6†ö–6W2uÕ³Õ²vÖW76vRuÕ²v6öçFVçBuĞĞ¢f–VÆG2Òö¢æÆöG2†6öçFVçE÷7G"Ğ¢&WGW&â§6öæ–g’‡²vö²s¢G'VRÂvf–VÆG2s¢f–VÆG2ÂwF–ÖRs¢æ÷u÷7G'ÒĞ¢W†6WBW†6WF–öâ2S Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢7G"†R—Ò’ÂS Ğ Ğ¤ç&÷WFR‚rö’ö'&–âöVÖ÷F–öå÷7FFRrÂÖWF†öG3Õ²ttUBuÒĞ¦FVb'&–åöVÖ÷F–öå÷7FFR‚“ Ğ¢G'“ Ğ¢–×÷'B7—22÷7—0Ğ¢÷7—2çF‚æ–ç6W'BƒÂrö÷Bög&öçFVæBrĞ¢–×÷'BVÖ÷F–öåöVæv–æR2öVPĞ¢7FFRÒöVRævWE÷7FFR‚Ğ¢Æöæv–ærÒöVRævWEöÆöæv–ær‚Ğ¢FW6—&RÒöVRævWEöFW6—&R‚Ğ¢&WGW&â§6öæ–g’‡²vö²s¢G'VRÂv7W'&VçBs¢°Ğ¢ws¢7FFRævWB‚wrÂãR’ÀĞ¢væs¢7FFRævWB‚værÂã"’ÀĞ¢wfÆVæ6Rs¢&÷VæB†æ÷&ÖÆ—¦U÷fÆVæ6R‡7FFRævWB‚wfÆVæ6RrÂãb’Â66ÆSÒwVæ—öÆ"r’Â2’ÀĞ¢v&÷W6Âs¢&÷VæB†æ÷&ÖÆ—¦Uö&÷W6Â‡7FFRævWB‚v&÷W6ÂrÂã2’’Â2’ÀĞ¢vÖööE÷v÷&Bs¢7FFRævWB‚vÖööE÷v÷&BrÂrr’ÀĞ¢vÆöæv–ærs¢&÷VæB†Æöæv–ærÂ2’ÀĞ¢wWFFVEöBs¢7FFRævWB‚wWFFVEöBrÂrr’ÀĞ¢w7FW&æ&W&u÷s¢FW6—&U²wuÒÀĞ¢w7FW&æ&W&uö’s¢FW6—&U²v’uÒÀĞ¢w7FW&æ&W&uö2s¢FW6—&U²v2uÒÀĞ¢×ÒĞ¢W†6WBW†6WF–öâ2S Ğ¢&WGW&â§6öæ–g’‡²vö²s¢fÇ6RÂvW'&÷"s¢7G"†R—Ò’ÂS Ğ Ğ Ğ¤ç&÷WFR‚rö’÷F†–æ²÷7VÖÖ&—¦RrÂÖWF†öG3Õ²uõ5BuÒĞ¦FVb7VÖÖ&—¦U÷F†–æ²‚“ Ğ¢FFÒ&WVW7BævWEö§6öâ‚’÷"·ĞĞ¢F†–æ¶–ærÒ†FFævWB‚wF†–æ¶–ærr’÷"rr’ç7G&—‚Ğ¢–bæ÷BF†–æ¶–æs Ğ¢&WGW&â§6öæ–g’‡²w7VÖÖ'’s¢rwÒĞ¢÷Ò°Ğ¢.Kº^Kˆ¾iŠşKˆjë^Xh^[ø>xºÎy›ŞûÈÎyJKˆX‹KŠNXú^KŠŞih~ûÈÎK‹®‹ùjë^h	Şˆ>X®KˆKŠ®j~š)[Èşy¨Nh¾{¹>ûÈÂ"ÀĞ¢.hÙ^hØ‹ùjë^h	Şˆ>˜xÎiÈkY>x8y¨Nh8^hIşx«nh8%Æâ"ÀĞ¢.ŠúŞŠˆzè{¸>8Šù~hHş8iÈX©¾[ªnûÈÎyJX[~KÙ>y¨NXªŠøŞY(ÎhIşZéŠøŞk~Yxëh8^hIşiÊÎ‹ª¾y¨NkŠ[ªnY(Î‹JYËûÈÂ"ÀĞ¢.ŠêŠû¾y¨NK«®ˆIZÙ˜xÎˆ;ŞX{®xëKˆKŠ®yK¾™Ú.8.[Ù>h	Şˆ>KŠŞiÈZûZ[‹ª¾KÙ>y¨Nk‹NiÉ¾8XÚiÈ8ˆ›.h8^y¨N[û^ZKNY(ÎXk.Xªi{nûÈÂ"ÀĞ¢.h¾{¹>K™şŠhŠù®ZéîYË[ŠnyØYÎzØkY>[ªny¨Nh8^jË.ûÈÎKˆŞh¨®jË.iÉ¾XÈ^Š8^h‰h©.h8^8""ÀĞ¢.Xú^ZÙˆ¨.ZXşXúş™[şXúşyúŞûÈÎzhjÚ.y»Ny›Şy¨Nh8^{º®ŠøŞZnz.ûÈÎzhjÚ.Xú>ŠúŞXÉnŠ‹ëî8%Æâ"ÀĞ¢.Kº^hIşXù~h‰nXh^[ø>XªKÙÎ[ÈZKNûÈÎzÊÎKˆK«®z{ŠxnŠy.KØnyÈyZ^K‹¾ŠúŞûÈh‰ûÈ8""ÀĞ¢.yJ™˜‹ûŠúŞk	Nh‰nXªŠøŞyúŞŠúŞûÈÎKˆŞXª[É^Xû~ûÈÎXú^Xû~{¹>[îûÈÎKˆŞ‹h^‹øs#ZÙ~8%ÆåÆâ"ÀĞ¢ĞĞ¢&ö×BÒ""æ¦ö–â…÷’².Xh^[ø>xºÎy›ŞûÉ¥Æâ"²F†–æ¶–æu³£#ĞĞ¢G'“ Ğ¢g&öÒ&VÆ’æÖævW"–×÷'B&VÆ”ÖævW Ğ¢g&öÒ6†Bç&W7öç6U÷'6W"–×÷'BW‡G&7E÷FW‡@Ğ¢&ÒÒ&VÆ”ÖævW"‚Ğ¢&BÒ&Òæ6ÆÂ‡°Ğ¢vÖ…÷Fö¶Vç2s¢#ÀĞ¢wF†–æ¶–ærs¢²wG—Rs¢vVæ&ÆVBrÂv'VFvWE÷Fö¶Vç2s¢#ÒÀĞ¢vÖW76vW2s¢·²w&öÆRs¢wW6W"rÂv6öçFVçBs¢&ö×GÕÒÀĞ¢ÒÂF–ÖV÷WCÓ#RĞ¢7VÖÖ'’ÒW‡G&7E÷FW‡B‡&BĞ¢&WGW&â§6öæ–g’‡²w7VÖÖ'’s¢7VÖÖ'’÷"rwÒĞ¢W†6WBW†6WF–öâ2S Ğ¢&WGW&â§6öæ–g’‡²w7VÖÖ'’s¢rrÂvW'&÷"s¢7G"†R—ÒĞ Ğ¤ç&÷WFR‚rö’ö6†B÷F†–æµ÷7VÖÖ'’rÂÖWF†öG3Õ²uõ5BuÒĞ¦FVb6fU÷F†–æµ÷7VÖÖ'’‚“ Ğ¢FFÒ&WVW7BævWEö§6öâ‚’÷"·ĞĞ¢×6uö–BÒFFævWB‚v×6uö–BrĞ¢7VÖÖ'’Ò†FFævWB‚w7VÖÖ'’r’÷"rr’ç7G&—‚Ğ¢–bæ÷B7VÖÖ'“ Ğ¢&WGW&â§6öæ–g’‡²vö²s¢fÇ6WÒĞ¢6öæâÒvWEöF"‚Ğ¢G'“ Ğ¢–b×6uö–C Ğ¢6öæâæW†V7WFR‚%UDDR6†EöÖW76vW24UBF†–æ¶–æu÷7VÖÖ'“Óòt„U$R–CÓò"Â‡7VÖÖ'’Â×6uö–B’Ğ¢VÇ6S Ğ¢6öæâæW†V7WFR‚%UDDR6†EöÖW76vW24UBF†–æ¶–æu÷7VÖÖ'“Óòt„U$RWF†÷"”â‚vg–öF÷"rÂv76—7FçBrÂv6ÆVFRr’õ$DU"%’–BDU42Ä”Ô•B"Â‡7VÖÖ'’Â’Ğ¢6öæâæ6öÖÖ—B‚Ğ¢f–æÆÇ“ Ğ¢6öæâæ6Æ÷6R‚Ğ¢&WGW&â§6öæ–g’‡²vö²s¢G'VWÒĞ Ğ Ğ¢2)H)Hv÷&·76R)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H)H Ğ¦–×÷'B7V'&ö6W722÷7ÂF†Æ–"2÷ÀĞ Ğ¥õu5õt„•DTÄ•5BÒ²rö÷Bög&öçFVæBrÂröWF2öæv–ç‚uĞĞ Ğ¦FVb÷w5öÆÆ÷vVB‡F‚“ Ğ¢Ò7G"…÷ÂåF‚‡F‚’ç&W6öÇfR‚’Ğ¢&WGW&âç’‡ÓÒr÷"ç7F'G7v—F‚‡r²ròr’f÷"r–âõu5õt„•DTÄ•5BĞ Ğ¤ç&÷WFR‚r÷v÷&·76RrĞ¦FVbv÷&·76U÷vR‚“ Ğ¢&WGW&â6VæEög&öÕöF—&V7F÷'’‚w7FF–2rÂwv÷&·76Ræ‡FÖÂrĞ Ğ¤ç&÷WFR‚rö’÷v÷&·76R÷G&VRrÂÖWF†öG3Õ²ttUBuÒĞ¦FVbw5÷G&VR‚“ Ğ¢–×÷'B÷0Ğ¢&ö÷BÒ&WVW7Bæ&w2ævWB‚vF—"rÂrö÷Bög&öçFVæBrĞ¢–bæ÷B÷w5öÆÆ÷vVB‡&ö÷B“ Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢væ÷BÆÆ÷vVBwÒ’ÂC0Ğ¢FVbö'V–ÆB‡F‚ÂFWFƒÓ“ Ğ¢—FV×2ÒµĞĞ¢G'“ Ğ¢VçG&–W2Ò6÷'FVB†÷2ç66æF—"‡F‚’Â¶W“ÖÆÖ&FS¢†æ÷BRæ—5öF—"‚’ÂRææÖR’Ğ¢W†6WBW&Ö—76–öäW'&÷# Ğ¢&WGW&â—FV×0Ğ¢f÷"R–âVçG&–W3 Ğ¢–bRææÖRç7F'G7v—F‚‚râr’æBRææÖRæ÷B–â‚ræVçbrÂ“¢6öçF–çVPĞ¢–bRææÖR–â‚uõ÷–66†UõòrÂvæöFUöÖöGVÆW2rÂræv—Br“¢6öçF–çVPĞ¢æöFRÒ²væÖRs¢RææÖRÂwF‚s¢RçF‚Âv—5öF—"s¢Ræ—5öF—"‚—ĞĞ¢–bRæ—5öF—"‚’æBFWF‚Â3 Ğ¢æöFU²v6†–ÆG&VâuÒÒö'V–ÆB†RçF‚ÂFWF‚³Ğ¢—FV×2æVæB†æöFRĞ¢&WGW&â—FV×0Ğ¢&WGW&â§6öæ–g’‡²wG&VRs¢ö'V–ÆB‡&ö÷B’Âw&ö÷Bs¢&ö÷GÒĞ Ğ¤ç&÷WFR‚rö’÷v÷&·76Röf–ÆRrÂÖWF†öG3Õ²ttUBuÒĞ¦FVbw5öf–ÆR‚“ Ğ¢F‚Ò&WVW7Bæ&w2ævWB‚wF‚rÂrrĞ¢–bæ÷BF‚÷"æ÷B÷w5öÆÆ÷vVB‡F‚“ Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢væ÷BÆÆ÷vVBwÒ’ÂC0Ğ¢G'“ Ğ¢v—F‚÷Vâ‡F‚Âw"rÂVæ6öF–æsÒwWFbÓ‚rÂW'&÷'3Òw&WÆ6Rr’2c Ğ¢6öçFVçBÒbç&VB‚Ğ¢&WGW&â§6öæ–g’‡²v6öçFVçBs¢6öçFVçBÂwF‚s¢F‚ÂvÆ–æW2s¢6öçFVçBæ6÷VçB‚uÆâr’²ÒĞ¢W†6WBW†6WF–öâ2S Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢7G"†R—Ò’ÂC Ğ Ğ¤ç&÷WFR‚rö’÷v÷&·76R÷w&—FRrÂÖWF†öG3Õ²uõ5BuÒĞ¦FVbw5÷w&—FR‚“ Ğ¢–×÷'BFFWF–ÖR2öG@Ğ¢FFÒ&WVW7BævWEö§6öâ‚’÷"·ĞĞ¢F‚ÒFFævWB‚wF‚rÂrrĞ¢6öçFVçBÒFFævWB‚v6öçFVçBrÂrrĞ¢–bæ÷BF‚÷"æ÷B÷w5öÆÆ÷vVB‡F‚“ Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢væ÷BÆÆ÷vVBwÒ’ÂC0Ğ¢G'“ Ğ¢&6·WÒF‚²rçw6&²pĞ¢G'“ Ğ¢–×÷'B6‡WF–Ã²6‡WF–Âæ6÷“"‡F‚Â&6·WĞ¢W†6WBW†6WF–öã¢70Ğ¢v—F‚÷Vâ‡F‚ÂwrrÂVæ6öF–æsÒwWFbÓ‚r’2c Ğ¢bçw&—FR†6öçFVçBĞ¢2ÆöpĞ¢6öæâÒvWEöF"‚Ğ¢6öæâæW†V7WFR‚$5$TDRD$ÄR”bäõBU„•5E2v÷&·76UöÆör†–B”åDTtU"$”Ô%’´U’UDô”ä5$TÔTåBÂF‚DU…BÂG2DDUD”ÔRDTdTÅB†FFWF–ÖR‚væ÷rrÂr³‚†÷W'2r’’’"Ğ¢6öæâæW†V7WFR‚$”å4U%B”åDòv÷&·76UöÆör‡F‚’dÅTU2ƒò’"Â‡F‚Â’Ğ¢6öæâæ6öÖÖ—B‚“²6öæâæ6Æ÷6R‚Ğ¢&WGW&â§6öæ–g’‡²vö²s¢G'VWÒĞ¢W†6WBW†6WF–öâ2S Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢7G"†R—Ò’ÂS Ğ Ğ¤ç&÷WFR‚rö’÷v÷&·76RöW†V2rÂÖWF†öG3Õ²uõ5BuÒĞ¦FVbw5öW†V2‚“ Ğ¢FFÒ&WVW7BævWEö§6öâ‚’÷"·ĞĞ¢6ÖBÒFFævWB‚v6ÖBrÂrrĞ¢ÄÄõtTBÒ²w7—7FVÖ7FÂ&W7F'Bg&öçFVæBrÂw7—7FVÖ7FÂ&W7F'Bg&öçFVæBÖwrrÀĞ¢w7—7FVÖ7FÂ&VÆöBg&öçFVæBrÂw7—7FVÖ7FÂ&VÆöBg&öçFVæBÖwrrÀĞ¢w7—7FVÖ7FÂ—2Ö7F—fRg&öçFVæBrÂw7—7FVÖ7FÂ—2Ö7F—fRg&öçFVæBÖwrrÀĞ¢w7—7FVÖ7FÂ7FGW2g&öçFVæBrÂw7—7FVÖ7FÂ7FGW2g&öçFVæBÖwrrÀĞ¢vv—BÔ2ö÷Bög&öçFVæB7FGW2rÂvv—BÔ2ö÷Bög&öçFVæBÆörÒÖöæVÆ–æRÓrÀĞ¢vv—BÔ2ö÷Bög&öçFVæBF–fbÒ×7FBuĞĞ¢–b6ÖBæ÷B–âÄÄõtTC Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢v6ÖBæ÷B–âÆÆ÷vÆ—7BwÒ’ÂC0Ğ¢G'“ Ğ¢"Ò÷7ç'Vâ†6ÖBç7Æ—B‚’Â6GW&Uö÷WGWCÕG'VRÂFW‡CÕG'VRÂF–ÖV÷WCÓRĞ¢&WGW&â§6öæ–g’‡²w7FF÷WBs¢"ç7FF÷WBÂw7FFW'"s¢"ç7FFW'"Âw&2s¢"ç&WGW&æ6öFWÒĞ¢W†6WBW†6WF–öâ2S Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢7G"†R—Ò’ÂS Ğ Ğ¤ç&÷WFR‚rö’÷v÷&·76R÷7FGW2rÂÖWF†öG3Õ²ttUBuÒĞ¦FVbw5÷7FGW2‚“ Ğ¢6W'f–6W2Ò·ĞĞ¢f÷"7f2–â²vg&öçFVæBrÂvg&öçFVæBÖwruÓ Ğ¢"Ò÷7ç'Vâ…²w7—7FVÖ7FÂrÂv—2Ö7F—fRrÂ7f5ÒÂ6GW&Uö÷WGWCÕG'VRÂFW‡CÕG'VRĞ¢6W'f–6W5·7f5ÒÒ"ç7FF÷WBç7G&—‚Ğ¢#"Ò÷7ç'Vâ…²vv—BrÂrÔ2rÂrö÷Bög&öçFVæBrÂvÆörrÂrÒÖöæVÆ–æRrÂrÓuÒÂ6GW&Uö÷WGWCÕG'VRÂFW‡CÕG'VRĞ¢#2Ò÷7ç'Vâ…²vv—BrÂrÔ2rÂrö÷Bög&öçFVæBrÂw7FGW2rÂrÒ×6†÷'BuÒÂ6GW&Uö÷WGWCÕG'VRÂFW‡CÕG'VRĞ¢&WGW&â§6öæ–g’‡²w6W'f–6W2s¢6W'f–6W2ÂvÆ7Eö6öÖÖ—Bs¢#"ç7FF÷WBç7G&—‚’Âvv—EöF—'G’s¢#2ç7FF÷WBç7G&—‚—ÒĞ Ğ Ğ¤ç&÷WFR‚rö’÷v÷&·76Rö6†BrÂÖWF†öG3Õ²uõ5BuÒĞ¦FVbw5ö6†B‚“ Ğ¢FFÒ&WVW7BævWEö§6öâ‚’÷"·ĞĞ¢ÖW76vRÒFFævWB‚vÖW76vRrÂrrĞ¢†—7F÷'’ÒFFævWB‚v†—7F÷'’rÅµÒĞ¢7W%öf–ÆRÒFFævWB‚vf–ÆRrÂrrĞ¢7W7FöÕöÖöFVÂÒ†FFævWB‚vÖöFVÂr’÷"rr’ç7G&—‚Ğ¢7—5÷&ö×BÒ~KÚiŠş‹KZZ^ZI®[	NûÈÎxëYÊYÊ[z^KÙÎXû[ŠîY8Zˆ^zêyee>Kˆ®y¨NX˜ŞzºşKº>z8.[z^KÙÎyºî[Ù^ûÉ¢ö÷Bög&öçFVæN8.Y¹îZHŞyJKŠŞih~8.Zh.iéÎ™ÈŠh[»®ŠêîXiXZ^ih~K»nûÈÎYÊY¹îZHŞ˜xÎyJ†w&—FS¢÷F‚÷Fòöf–ÆUÆîikXh^Zë•ÆæjÎ[ÈşXÈ^Š;8"pĞ¢×6w2Ò¶Òf÷"Ò–â†—7F÷'•²Ó¥Ò–bÒævWB‚w&öÆRr’æBÒævWB‚v6öçFVçBr•ĞĞ¢–bæ÷B×6w2÷"×6w5²ÓÒævWB‚w&öÆRr’ÒwW6W"s Ğ¢×6w2æVæB‡²w&öÆRs¢wW6W"rÂv6öçFVçBs¦ÖW76vWÒĞ¢G'“ Ğ¢g&öÒ&VÆ’æÖævW"–×÷'B&VÆ”ÖævW Ğ¢g&öÒ6†Bç&W7öç6U÷'6W"–×÷'BW‡G&7E÷FW‡@Ğ¢&ÒÒ&VÆ”ÖævW"‚Ğ¢–ÆöBÒ²vÖ…÷Fö¶Vç2s£#Âw7—7FVÒs§7—5÷&ö×BÂvÖW76vW2s¦×6w7ĞĞ¢–b7W7FöÕöÖöFVÃ Ğ¢–ÆöE²vÖöFVÂuÒÒ7W7FöÕöÖöFVÀĞ¢&BÒ&Òæ6ÆÂ‡–ÆöBÂF–ÖV÷WCÓ#Ğ¢&WÇ’ÒW‡G&7E÷FW‡B‡&BĞ¢&WGW&â§6öæ–g’‡²w&WÇ’s¢&WÇ—ÒĞ¢W†6WBW†6WF–öâ2S Ğ¢&WGW&â§6öæ–g’‡²vW'&÷"s¢7G"†R’Âw&WÇ’s¢~Šû~k.ZK‹JS¢r·7G"†R—ÒĞ Ğ¤ç&÷WFR‚rö’ö'&–âöG&—fU÷7FFRrÂÖWF†öG3Õ²ttUBuÒĞ¦FVb'&–åöG&—fU÷7FFR‚“ Ğ¢G'“ Ğ¢–×÷'B7—22÷7—0Ğ¢÷7—2çF‚æ–ç6W'BƒÂrö÷Bög&öçFVæBrĞ¢–×÷'BG&—fUöVæv–æR2öFPĞ¢G&—fRÒöFRævWEöG&—fR‚Ğ¢FV6—6–öâÒöFRæFV6–FR‚Ğ¢&WGW&â§6öæ–g’‡²vö²s¢G'VRÂvG&—fRs¢G&—fRÂvFV6—6–öâs¢°Ğ¢vf—&VBs¢FV6—6–öå²vf—&VBuÒÀĞ¢v7F–öâs¢FV6—6–öå²v7F–öâuÒÀĞ¢v&Æö6¶VBs¢FV6—6–öå²v&Æö6¶VBuÒÀĞ¢v†–çBs¢FV6—6–öå²v†–çBuÒÀĞ¢×ÒĞ¢W†6WBW†6WF–öâ2S Ğ¢&WGW&â§6öæ–g’‡²vö²s¢fÇ6RÂvW'&÷"s¢7G"†R—Ò’ÂS Ğ