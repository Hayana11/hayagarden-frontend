import ast
import hashlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Import-time _init() must never touch the production Gallery during tests.
os.environ['HAYAGARDEN_GALLERY_ROOT'] = tempfile.mkdtemp(prefix='gallery-r1-tests-')
import gallery_store
gallery_store.DB_PATH = os.path.join(os.environ['HAYAGARDEN_GALLERY_ROOT'], 'gallery.db')
gallery_store.GALLERY_DIR = os.path.join(os.environ['HAYAGARDEN_GALLERY_ROOT'], 'gallery')
gallery_store._init()


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _png_bytes(seed=b'pixels'):
    return b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0d' + b'IHDR' + struct.pack('>II', 3, 2) + seed


@pytest.fixture
def isolated_gallery(tmp_path, monkeypatch):
    monkeypatch.setattr(gallery_store, 'DB_PATH', str(tmp_path / 'gallery.db'))
    monkeypatch.setattr(gallery_store, 'GALLERY_DIR', str(tmp_path / 'gallery'))
    gallery_store._init()
    return tmp_path


def _turn_db(path, message_id=41, attachments=None, content='爸爸看这个', author='hayana'):
    path.mkdir(parents=True, exist_ok=True)
    db_path = str(path / 'messages.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE chat_messages (
        id INTEGER PRIMARY KEY, author TEXT, content TEXT, attachments TEXT, image_url TEXT
    )''')
    conn.execute('INSERT INTO chat_messages VALUES (?,?,?,?,?)', (
        message_id, author, content, json.dumps(attachments or []), '',
    ))
    conn.commit()
    conn.close()

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    return get_db


def _extract_function(file_path, function_name, namespace):
    tree = ast.parse(Path(file_path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == function_name)
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(file_path), 'exec'), namespace)
    return namespace[function_name]


def test_legacy_attachment_uri_remains_saveable(isolated_gallery, tmp_path, monkeypatch):
    import attachment_store
    image = _png_bytes()
    attach_dir = tmp_path / 'attachments'
    attach_dir.mkdir()
    (attach_dir / 'deadbeef.png').write_bytes(image)
    monkeypatch.setattr(attachment_store, 'ATTACH_DIR', str(attach_dir), raising=False)
    monkeypatch.setattr(attachment_store, 'get', lambda aid: {
        'filename': 'deadbeef.png', 'mime': 'image/png', 'created_at': '2026-09-26 09:00:00',
    })

    result = gallery_store.save_attachment('attachment://deadbeef', note='截图')

    assert result and result['pid']
    assert gallery_store.read_photo_bytes(result['pid'])[0] == image


def test_current_turn_direct_upload_is_selected_by_index(isolated_gallery, tmp_path):
    from chat.gallery_context import resolve_current_turn_image
    attachments = [
        {'type': 'image', 'url': '/static/uploads/first.png'},
        {'type': 'image', 'url': '/static/uploads/second.png'},
    ]
    get_db = _turn_db(tmp_path, message_id=42, attachments=attachments)

    selected = resolve_current_turn_image(42, 'chat-main', get_db_fn=get_db, image_index=1)

    assert selected == {
        'ref': '/static/uploads/second.png', 'image_index': 1,
        'source_msg_id': 42, 'source_chat_id': 'chat-main',
    }


def test_current_turn_save_binds_exact_message_and_keeps_impression(isolated_gallery, tmp_path, monkeypatch):
    from chat import cc_vision_bridge
    image = _png_bytes(b'current')
    attachments = [{'type': 'image', 'url': '/static/uploads/current.png'}]
    get_db = _turn_db(tmp_path, message_id=43, attachments=attachments)
    monkeypatch.setattr(cc_vision_bridge, 'resolve_image_bytes', lambda ref: (image, 'image/png'))
    monkeypatch.setattr(cc_vision_bridge, 'DEFAULT_UPLOAD_DIR', str(tmp_path))
    namespace = {
        '_tool_ctx': SimpleNamespace(user_message_id=43, conversation_id='chat-main'),
        'get_db': get_db, 'json': json,
        '_gen_gallery_visual_description': lambda pid: '蓝色围巾挂在木椅上。',
        '_gen_photo_meaning': lambda **kwargs: None,
    }
    save = _extract_function(PROJECT_ROOT / 'gateway.py', '_save_to_gallery', namespace)

    raw_result = save('', '备注', None, None, '那时我觉得很安心。')

    result = json.loads(raw_result)
    photo = gallery_store.get(result['pid'])
    assert result['ok'] is True
    assert photo['source_msg_id'] == 43
    assert photo['source_chat_id'] == 'chat-main'
    assert photo['first_impression'] == '那时我觉得很安心。'
    assert photo['visual_description'] == '蓝色围巾挂在木椅上。'


def test_missing_or_ambiguous_current_turn_image_fails_closed(isolated_gallery, tmp_path):
    from chat.gallery_context import CurrentTurnImageError, resolve_current_turn_image
    attachments = [
        {'type': 'image', 'url': '/static/uploads/one.png'},
        {'type': 'image', 'url': '/static/uploads/two.png'},
    ]
    get_db = _turn_db(tmp_path, message_id=44, attachments=attachments)
    with pytest.raises(CurrentTurnImageError, match='多张图片'):
        resolve_current_turn_image(44, 'chat-main', get_db_fn=get_db)
    with pytest.raises(CurrentTurnImageError, match='不存在'):
        resolve_current_turn_image(999, 'chat-main', get_db_fn=get_db, image_index=0)


def test_current_turn_binding_requires_user_message_and_chat_id(isolated_gallery, tmp_path):
    from chat.gallery_context import CurrentTurnImageError, resolve_current_turn_image
    attachment = [{'type': 'image', 'url': '/static/uploads/one.png'}]
    assistant_db = _turn_db(tmp_path / 'assistant', message_id=45, attachments=attachment,
                             author='assistant')
    with pytest.raises(CurrentTurnImageError, match='不是用户消息'):
        resolve_current_turn_image(45, 'chat-main', get_db_fn=assistant_db, image_index=0)

    user_db = _turn_db(tmp_path / 'user', message_id=46, attachments=attachment)
    with pytest.raises(CurrentTurnImageError, match='会话标识缺失'):
        resolve_current_turn_image(46, '', get_db_fn=user_db, image_index=0)


def test_gallery_copy_survives_temporary_attachment_removal(isolated_gallery, tmp_path):
    import attachment_store
    source = _png_bytes(b'permanent-copy')
    attach_dir = tmp_path / 'temporary-attachments'
    attach_dir.mkdir()
    (attach_dir / 'cafebabe.png').write_bytes(source)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(attachment_store, 'ATTACH_DIR', str(attach_dir), raising=False)
    monkeypatch.setattr(attachment_store, 'get', lambda aid: {
        'filename': 'cafebabe.png', 'mime': 'image/png',
    })
    try:
        result = gallery_store.save_attachment('attachment://cafebabe')
    finally:
        monkeypatch.undo()
    (attach_dir / 'cafebabe.png').unlink()

    assert gallery_store.read_photo_bytes(result['pid'])[0] == source


def test_content_hash_is_sha256_of_persisted_bytes(isolated_gallery):
    image = _png_bytes(b'exact-bytes')
    result = gallery_store.save_image_bytes(image, 'image/png')
    assert result['content_hash'] == hashlib.sha256(image).hexdigest()
    assert gallery_store.get(result['pid'])['content_hash'] == hashlib.sha256(image).hexdigest()


def test_exact_duplicate_reuses_pid_without_second_file(isolated_gallery):
    image = _png_bytes(b'duplicate')
    first = gallery_store.save_image_bytes(image, 'image/png', note='第一条')
    second = gallery_store.save_image_bytes(image, 'image/png', note='第二条')
    files = list(Path(gallery_store.GALLERY_DIR).iterdir())
    assert second['reused_existing'] is True
    assert second['pid'] == first['pid']
    assert len(files) == 1


def test_duplicate_preserves_note_impression_and_album(isolated_gallery):
    image = _png_bytes(b'metadata-stays')
    first = gallery_store.save_image_bytes(
        image, 'image/png', note='原备注', album_name='第一次', first_impression='原始印象',
    )
    second = gallery_store.save_image_bytes(
        image, 'image/png', note='新备注', album_name='另一本', first_impression='新印象',
    )
    saved = gallery_store.get(first['pid'])
    assert second['reused_existing'] is True
    assert saved['note'] == '原备注'
    assert saved['first_impression'] == '原始印象'
    assert saved['album_id'] == first['album_id']


def test_neutral_visual_worker_receives_pixels_without_persona(isolated_gallery):
    from chat.gallery_visual import describe_image_bytes
    image = _png_bytes(b'vision')
    seen = {}

    class Relay:
        def call(self, payload, timeout):
            seen['payload'] = payload
            seen['timeout'] = timeout
            return {'text': '一只猫坐在窗边。'}

    description = describe_image_bytes(
        image, 'image/png', relay_client=Relay(), extract_text_fn=lambda value: value['text'],
    )
    user_content = seen['payload']['messages'][0]['content']
    assert description == '一只猫坐在窗边。'
    assert user_content[0]['type'] == 'image'
    assert '猫' not in seen['payload']['system']
    assert '费奥多尔' not in seen['payload']['system']
    assert seen['timeout'] == 15


def test_vision_failure_does_not_undo_saved_photo(isolated_gallery):
    from chat.gallery_visual import describe_image_bytes
    image = _png_bytes(b'vision-fails')
    saved = gallery_store.save_image_bytes(image, 'image/png', first_impression='很温暖')

    class BrokenRelay:
        def call(self, payload, timeout):
            raise TimeoutError('vision timeout')

    assert describe_image_bytes(
        image, 'image/png', relay_client=BrokenRelay(), extract_text_fn=lambda value: '',
    ) is None
    photo = gallery_store.get(saved['pid'])
    assert photo is not None
    assert gallery_store.read_photo_bytes(saved['pid'])[0] == image
    assert photo['first_impression'] == '很温暖'


def test_old_gallery_row_migrates_and_remains_readable(isolated_gallery, tmp_path):
    image = _png_bytes(b'legacy-row')
    photo_path = Path(gallery_store.GALLERY_DIR) / 'legacy.png'
    photo_path.write_bytes(image)
    conn = sqlite3.connect(gallery_store.DB_PATH)
    conn.execute('DROP TABLE gallery_photos')
    conn.execute('''CREATE TABLE gallery_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, pid TEXT UNIQUE NOT NULL,
        album_id INTEGER, storage_key TEXT NOT NULL, mime TEXT DEFAULT 'image/png',
        width INTEGER, height INTEGER, note TEXT DEFAULT '', source_type TEXT DEFAULT '',
        source_msg_id INTEGER, source_chat_id INTEGER, tags TEXT DEFAULT '[]',
        favorite INTEGER DEFAULT 0, created_at TEXT, saved_at TEXT
    )''')
    conn.execute('''INSERT INTO gallery_photos
        (pid, album_id, storage_key, mime, note, source_type, created_at, saved_at)
        VALUES (?,?,?,?,?,?,?,?)''',
        ('legacy', 1, 'legacy.png', 'image/png', '旧备注', 'chat', '2024-01-01', '2024-01-02'))
    conn.commit()
    conn.close()
    gallery_store._init()
    row = gallery_store.get('legacy')
    assert row['note'] == '旧备注'
    assert row['visual_description'] == ''
    assert row['first_impression'] == ''
    assert row['content_hash'] is None
    assert gallery_store.read_photo_bytes('legacy')[0] == image
    reused = gallery_store.save_image_bytes(image, 'image/png', note='不要覆盖旧备注')
    assert reused['reused_existing'] is True
    assert reused['pid'] == 'legacy'
    assert gallery_store.get('legacy')['note'] == '旧备注'
    assert len(list(Path(gallery_store.GALLERY_DIR).iterdir())) == 1
    columns = {r[1] for r in sqlite3.connect(gallery_store.DB_PATH).execute(
        'PRAGMA table_info(gallery_photos)').fetchall()}
    assert {'content_hash', 'visual_description', 'first_impression', 'send_count', 'first_sent_at'} <= columns


def test_database_insert_failure_removes_partial_permanent_file(isolated_gallery):
    conn = sqlite3.connect(gallery_store.DB_PATH)
    conn.execute('''CREATE TRIGGER reject_gallery_insert BEFORE INSERT ON gallery_photos
        BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END''')
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError, match='injected insert failure'):
        gallery_store.save_image_bytes(_png_bytes(b'rollback'), 'image/png')
    assert list(Path(gallery_store.GALLERY_DIR).iterdir()) == []
    assert gallery_store.count_photos() == 0


def test_concurrent_exact_saves_claim_one_row_and_one_file(isolated_gallery):
    from concurrent.futures import ThreadPoolExecutor
    image = _png_bytes(b'racing-duplicate')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: gallery_store.save_image_bytes(image, 'image/png'), range(2)))
    assert len({item['pid'] for item in results}) == 1
    assert sorted(item['reused_existing'] for item in results) == [False, True]
    assert gallery_store.count_photos() == 1
    assert len(list(Path(gallery_store.GALLERY_DIR).iterdir())) == 1


def test_search_includes_visual_description_and_first_impression(isolated_gallery):
    a = gallery_store.save_image_bytes(_png_bytes(b'search-a'), 'image/png')
    b = gallery_store.save_image_bytes(_png_bytes(b'search-b'), 'image/png')
    gallery_store.set_meaning(a['pid'], visual_description='黄色雨伞靠在门边')
    gallery_store.set_meaning(b['pid'], first_impression='那天我忽然想起海边')
    assert [p['pid'] for p in gallery_store.search_photos('黄色雨伞')] == [a['pid']]
    assert [p['pid'] for p in gallery_store.search_photos('海边')] == [b['pid']]


def test_gallery_api_returns_visual_memory_fields_without_storage_path(isolated_gallery):
    photo = gallery_store.save_image_bytes(_png_bytes(b'api'), 'image/png', note='旧照片')
    gallery_store.set_meaning(photo['pid'], visual_description='窗台上的小花',
                              first_impression='我当时觉得安静')
    app_path = PROJECT_ROOT / 'app.py'
    fake_request = SimpleNamespace(args=SimpleNamespace(get=lambda key, default=None, **kwargs: default))
    namespace = {
        'request': fake_request,
        'jsonify': lambda payload: payload,
        'gallery_store': gallery_store,
        'json': json,
    }
    route = _extract_function(app_path, 'gallery_photos_list', namespace)
    payload = route()
    result = next(p for p in payload['photos'] if p['pid'] == photo['pid'])
    assert result['visual_description'] == '窗台上的小花'
    assert result['first_impression'] == '我当时觉得安静'
    assert 'storage_key' not in result


def test_recall_labels_lossy_semantic_memory_and_preserves_marker(isolated_gallery, monkeypatch):
    photo = gallery_store.save_image_bytes(_png_bytes(b'recall'), 'image/png', note='旧图')
    gallery_store.set_meaning(photo['pid'], summary='那次散步让我安心',
                              visual_description='两个人影走在树下',
                              first_impression='我想把那段安静留住',
                              keywords=['散步'])
    namespace = {'json': json, '_gen_gallery_visual_description': lambda pid, question=None: None}
    recall = _extract_function(PROJECT_ROOT / 'gateway.py', '_recall_photo', namespace)

    result = json.loads(recall(pid=photo['pid']))

    assert result['semantic_memory_only'] is True
    assert result['original_reloaded'] is False
    assert result['source_msg_id'] is None
    assert result['delivery_marker'] == f"[[gallery:{photo['pid']}]]"
    assert result['visual_description'] == '两个人影走在树下'


def test_moments_maps_and_separates_visual_and_subjective_fields():
    moments_source = (PROJECT_ROOT / 'app/src/lib/moments.ts').read_text(encoding='utf-8')
    screen_source = (PROJECT_ROOT / 'app/src/screens/MomentsScreen.tsx').read_text(encoding='utf-8')
    assert 'visualDescription: p.visual_description ||' in moments_source
    assert 'firstImpression: p.first_impression ||' in moments_source
    assert '画面</div>' in screen_source
    assert '那时</div>' in screen_source
    assert '记得</div>' in screen_source
    assert "width: 'min(560px,92vw)'" not in screen_source
    assert 'height={g.height' not in screen_source
    assert 'height={lightbox.height' not in screen_source


def test_moments_empty_album_state_remains_honest():
    screen_source = (PROJECT_ROOT / 'app/src/screens/MomentsScreen.tsx').read_text(encoding='utf-8')
    assert "(data?.gallery || []).length === 0" in screen_source
    assert '还没有存进相册的照片' in screen_source


def test_chat_stream_binds_exact_user_message_id_for_gallery_tool():
    source = (PROJECT_ROOT / 'gateway.py').read_text(encoding='utf-8')
    assert "_tool_ctx.user_message_id = _turn_data.get('user_message_id')" in source
    assert "del _tool_ctx.user_message_id" in source


def test_vision_failure_keeps_gateway_save_successful(isolated_gallery, tmp_path, monkeypatch):
    from chat import cc_vision_bridge
    image = _png_bytes(b'gateway-vision-failure')
    get_db = _turn_db(tmp_path, message_id=77, attachments=[
        {'type': 'image', 'url': '/static/uploads/current.png'},
    ])
    monkeypatch.setattr(cc_vision_bridge, 'resolve_image_bytes', lambda ref: (image, 'image/png'))
    namespace = {
        '_tool_ctx': SimpleNamespace(user_message_id=77, conversation_id='chat-main'),
        'get_db': get_db, 'json': json,
        '_gen_gallery_visual_description': lambda pid: None,
        '_gen_photo_meaning': lambda **kwargs: None,
    }
    save = _extract_function(PROJECT_ROOT / 'gateway.py', '_save_to_gallery', namespace)

    result = json.loads(save(first_impression='我想留下这一刻'))

    assert result['ok'] is True
    assert result['visual_description_available'] is False
    saved = gallery_store.get(result['pid'])
    assert saved['first_impression'] == '我想留下这一刻'
    assert gallery_store.read_photo_bytes(result['pid'])[0] == image
