"""P0 Claude Code vision bridge — narrow unit tests T1–T8."""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFont

from chat.cc_vision_bridge import (
    VisionBridgeError,
    assert_claude_user_content_safe,
    build_claude_user_content,
    build_image_content_block,
    resolve_image_bytes,
    summarize_content_shape,
)
from tools.claude_forge_core import (
    ForgeOptions,
    forge_transcript,
    new_uuid,
)


MARKER = 'HAYA_VISION_7319'


def _make_png(path: Path, text: str = MARKER) -> None:
    img = Image.new('RGB', (320, 100), 'white')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 28,
        )
    except Exception:
        font = ImageFont.load_default()
    draw.text((16, 30), text, fill='black', font=font)
    img.save(path, format='PNG')


class VisionBridgeUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upload_dir = self.root / 'uploads'
        self.attach_dir = self.root / 'attachments'
        self.upload_dir.mkdir()
        self.attach_dir.mkdir()
        self.png = self.upload_dir / 'probe_vision.png'
        _make_png(self.png)

    def tearDown(self):
        self.tmp.cleanup()

    def _resolve(self, ref: str):
        return resolve_image_bytes(
            ref,
            upload_dir=str(self.upload_dir),
            attach_dir=str(self.attach_dir),
            get_attachment=self._get_attachment,
        )

    def _get_attachment(self, aid: str):
        # Simple fake: id maps to <id>.png in attach_dir when present.
        fname = aid + '.png'
        path = self.attach_dir / fname
        if not path.is_file():
            return None
        return {
            'id': aid,
            'filename': fname,
            'kind': 'image',
            'mime': 'image/png',
        }

    # --- T1 text-only regression ---
    def test_t1_text_only_returns_plain_string(self):
        out = build_claude_user_content(text='hello', image_refs=None)
        self.assertEqual(out, 'hello')
        self.assertIsInstance(out, str)
        # Exact payload shape used by ResidentSession.send_turn
        payload = {
            'type': 'user',
            'message': {'role': 'user', 'content': out},
        }
        self.assertEqual(payload['message']['content'], 'hello')

    # --- T2 text + PNG ---
    def test_t2_text_plus_png(self):
        ref = '/static/uploads/probe_vision.png'
        out = build_claude_user_content(
            text='看看这个',
            image_refs=[ref],
            resolve_fn=self._resolve,
        )
        self.assertIsInstance(out, list)
        types = [b.get('type') for b in out]
        self.assertIn('text', types)
        self.assertIn('image', types)
        text_block = next(b for b in out if b['type'] == 'text')
        img_block = next(b for b in out if b['type'] == 'image')
        self.assertEqual(text_block['text'], '看看这个')
        self.assertEqual(img_block['source']['type'], 'base64')
        self.assertEqual(img_block['source']['media_type'], 'image/png')
        self.assertTrue(img_block['source']['data'])
        # Must not leak uncontrolled paths into the payload
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn(str(self.upload_dir), blob)
        self.assertNotIn('/opt/frontend/attachments', blob)
        assert_claude_user_content_safe(out)

    # --- T3 image-only ---
    def test_t3_image_only(self):
        ref = '/static/uploads/probe_vision.png'
        out = build_claude_user_content(
            text='',
            image_refs=[ref],
            resolve_fn=self._resolve,
        )
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['type'], 'image')
        self.assertTrue(out[0]['source']['data'])

        # Placeholder must not discard the image
        out2 = build_claude_user_content(
            text='[image]',
            image_refs=[ref],
            resolve_fn=self._resolve,
        )
        self.assertEqual(len(out2), 1)
        self.assertEqual(out2[0]['type'], 'image')

    # --- T4 missing attachment ---
    def test_t4_missing_attachment_fails_closed(self):
        with self.assertRaises(VisionBridgeError) as ar:
            build_claude_user_content(
                text='x',
                image_refs=['attachment://deadbeef'],
                resolve_fn=self._resolve,
            )
        self.assertEqual(ar.exception.code, 'missing_attachment')

        # Empty image block must never be produced
        with self.assertRaises(VisionBridgeError):
            build_image_content_block(b'', 'image/png')
        with self.assertRaises(VisionBridgeError):
            assert_claude_user_content_safe([
                {'type': 'image', 'source': {
                    'type': 'base64', 'media_type': 'image/png', 'data': '',
                }},
            ])

    # --- T5 invalid MIME / empty image ---
    def test_t5_invalid_mime_and_empty_file(self):
        bad = self.upload_dir / 'note.txt'
        bad.write_text('not an image', encoding='utf-8')
        with self.assertRaises(VisionBridgeError) as ar:
            resolve_image_bytes(
                '/static/uploads/note.txt',
                upload_dir=str(self.upload_dir),
            )
        self.assertEqual(ar.exception.code, 'invalid_mime')

        empty = self.upload_dir / 'empty.png'
        empty.write_bytes(b'')
        with self.assertRaises(VisionBridgeError) as ar2:
            resolve_image_bytes(
                '/static/uploads/empty.png',
                upload_dir=str(self.upload_dir),
            )
        self.assertIn(ar2.exception.code, {'empty_image', 'invalid_mime'})

        # Path traversal / symlink escape
        with self.assertRaises(VisionBridgeError):
            resolve_image_bytes(
                '/static/uploads/../etc/passwd',
                upload_dir=str(self.upload_dir),
            )

    # --- T6 hot resident: one user JSONL write, no respawn ---
    def test_t6_hot_resident_image_turn_no_respawn(self):
        import cc_resident

        ref = '/static/uploads/probe_vision.png'
        content = build_claude_user_content(
            text='看图',
            image_refs=[ref],
            resolve_fn=self._resolve,
        )

        writes: list[str] = []

        class _FakeStdin:
            def write(self, data):
                writes.append(data)
                return len(data)

            def flush(self):
                return None

        class _FakeStdout:
            def __init__(self):
                self._lines = [
                    json.dumps({'type': 'assistant', 'message': {
                        'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'ok'}],
                    }}) + '\n',
                    json.dumps({
                        'type': 'result', 'subtype': 'success',
                        'is_error': False, 'result': 'ok',
                        'session_id': 'sess-vision',
                        'usage': {
                            'input_tokens': 1, 'output_tokens': 1,
                            'cache_read_input_tokens': 0,
                            'cache_creation_input_tokens': 0,
                        },
                    }) + '\n',
                    '',
                ]
                self._i = 0

            def readline(self):
                if self._i >= len(self._lines):
                    return ''
                line = self._lines[self._i]
                self._i += 1
                return line

        class _FakeProc:
            def __init__(self):
                self.stdin = _FakeStdin()
                self.stdout = _FakeStdout()
                self.stderr = io.StringIO()
                self._alive = True

            def poll(self):
                return None if self._alive else 0

        rs = cc_resident.ResidentSession(
            cwd=str(self.root),
            mcp_config_path=str(self.root / 'mcp.json'),
            allowed_tools='',
        )
        fake = _FakeProc()
        rs._proc = fake
        rs._session_id = 'sess-vision'
        rs._cold = False
        rs._generation = 3
        kill_calls = []
        rs._kill = lambda quiet=False: kill_calls.append(quiet) or setattr(fake, '_alive', False)

        events = list(rs.send_turn(content))
        self.assertEqual(len(writes), 1)
        payload = json.loads(writes[0].strip())
        self.assertEqual(payload['type'], 'user')
        self.assertIsInstance(payload['message']['content'], list)
        self.assertTrue(any(
            b.get('type') == 'image' for b in payload['message']['content']
        ))
        # No respawn/kill from vision turn itself
        self.assertEqual(kill_calls, [])
        self.assertTrue(any(e[0] == 'done' for e in events))

        # Follow-up text turn still works on same proc
        writes.clear()
        fake.stdout = _FakeStdout()
        events2 = list(rs.send_turn('继续文字'))
        self.assertEqual(len(writes), 1)
        p2 = json.loads(writes[0].strip())
        self.assertEqual(p2['message']['content'], '继续文字')
        self.assertTrue(any(e[0] == 'done' for e in events2))
        self.assertEqual(kill_calls, [])

    def test_t6_missing_image_does_not_write_stdin(self):
        import cc_resident

        writes: list[str] = []

        class _FakeStdin:
            def write(self, data):
                writes.append(data)
                return len(data)

            def flush(self):
                return None

        class _FakeProc:
            def __init__(self):
                self.stdin = _FakeStdin()
                self.stdout = io.StringIO()
                self.stderr = io.StringIO()

            def poll(self):
                return None

        rs = cc_resident.ResidentSession(
            cwd=str(self.root),
            mcp_config_path=str(self.root / 'mcp.json'),
            allowed_tools='',
        )
        rs._proc = _FakeProc()
        rs._session_id = 'sess'
        rs._cold = False
        bad = [{
            'type': 'image',
            'source': {'type': 'base64', 'media_type': 'image/png', 'data': ''},
        }]
        with self.assertRaises(cc_resident.ResidentError):
            list(rs.send_turn(bad))
        self.assertEqual(writes, [])

    # --- T7 Forge multimodal preservation ---
    def test_t7_forge_preserves_image_blocks(self):
        data = base64.standard_b64encode(self.png.read_bytes()).decode('ascii')
        uid = new_uuid()
        aid = new_uuid()
        sid = new_uuid()
        src = [
            {
                'type': 'user',
                'uuid': uid,
                'parentUuid': None,
                'timestamp': '2026-08-07T00:00:00.000Z',
                'sessionId': sid,
                'cwd': '/tmp/x',
                'version': '1',
                'message': {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': '【昨日延续对话】\n看看这个'},
                        {
                            'type': 'image',
                            'source': {
                                'type': 'base64',
                                'media_type': 'image/png',
                                'data': data,
                            },
                        },
                    ],
                },
            },
            {
                'type': 'assistant',
                'uuid': aid,
                'parentUuid': uid,
                'timestamp': '2026-08-07T00:00:01.000Z',
                'sessionId': sid,
                'cwd': '/tmp/x',
                'version': '1',
                'message': {
                    'role': 'assistant',
                    'content': [{'type': 'text', 'text': '看到了'}],
                },
            },
        ]
        forged = forge_transcript(
            src,
            ForgeOptions(
                new_session_id=new_uuid(),
                cwd='/tmp/x',
                user_canonical_by_event_uuid={uid: '看看这个'},
            ),
        )
        user_content = forged.events[0]['message']['content']
        self.assertIsInstance(user_content, list)
        text_blocks = [b for b in user_content if b.get('type') == 'text']
        img_blocks = [b for b in user_content if b.get('type') == 'image']
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(text_blocks[0]['text'], '看看这个')
        self.assertEqual(len(img_blocks), 1)
        self.assertEqual(img_blocks[0]['source']['data'], data)
        self.assertEqual(img_blocks[0]['source']['media_type'], 'image/png')

    def test_t7_forge_drops_empty_image_safely(self):
        uid = new_uuid()
        sid = new_uuid()
        src = [{
            'type': 'user',
            'uuid': uid,
            'parentUuid': None,
            'timestamp': '2026-08-07T00:00:00.000Z',
            'sessionId': sid,
            'cwd': '/tmp/x',
            'version': '1',
            'message': {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': '看看这个'},
                    {
                        'type': 'image',
                        'source': {
                            'type': 'base64',
                            'media_type': 'image/png',
                            'data': '',
                        },
                    },
                ],
            },
        }]
        forged = forge_transcript(
            src,
            ForgeOptions(
                new_session_id=new_uuid(),
                cwd='/tmp/x',
                user_canonical_by_event_uuid={uid: '看看这个'},
            ),
        )
        # Empty image dropped → text-only string (safe degrade)
        self.assertEqual(forged.events[0]['message']['content'], '看看这个')

    # --- T8 Forge text-only CASE 7 regression ---
    def test_t8_forge_case7_text_only_regression(self):
        from tools.claude_forge_validator import validate_forged_transcript

        fixture_root = (
            Path(__file__).resolve().parents[1]
            / 'tests' / 'fixtures' / 'claude_forge_spike'
        )
        if not fixture_root.is_dir():
            # Alternate layout used by existing suite
            fixture_root = (
                Path(__file__).resolve().parents[1]
                / 'artifacts' / 'spike-claude-forge-resume'
            )
        case7 = fixture_root / 'case7_legacy_injection.jsonl'
        canon_path = fixture_root / 'app_db_canonical_messages.json'
        if not case7.is_file():
            # Fall back to importing the existing test module's path discovery
            from tests.test_claude_forge_spike import FIXTURE_ROOT
            case7 = FIXTURE_ROOT / 'case7_legacy_injection.jsonl'
            canon_path = FIXTURE_ROOT / 'app_db_canonical_messages.json'

        from tools.claude_forge_core import load_jsonl

        src = load_jsonl(case7)
        canon = json.loads(canon_path.read_text(encoding='utf-8'))
        inj_uuid = next(str(e.get('uuid')) for e in src if e.get('type') == 'user')
        forged = forge_transcript(
            src,
            ForgeOptions(
                new_session_id=new_uuid(),
                cwd='/tmp/x',
                user_canonical_by_event_uuid={
                    inj_uuid: canon['by_claude_event_uuid'][inj_uuid],
                },
            ),
        )
        user_text = forged.events[0]['message']['content']
        self.assertEqual(user_text, '今天天气怎么样')
        result = validate_forged_transcript(
            forged.events, session_id=forged.events[0]['sessionId'],
        )
        self.assertTrue(result.ok, result.errors)


class DailyRuntimeVisionWiringTests(unittest.TestCase):
    """format_resident_turn_content must attach image for Daily Soft Window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self.tmp.name) / 'uploads'
        self.upload_dir.mkdir()
        self.png = self.upload_dir / 'hot_img.png'
        _make_png(self.png)

    def tearDown(self):
        self.tmp.cleanup()

    def test_daily_hot_turn_becomes_multimodal(self):
        from chat.daily_runtime import format_resident_turn_content
        from chat import cc_vision_bridge as vb

        def _resolve(ref):
            return resolve_image_bytes(ref, upload_dir=str(self.upload_dir))

        with mock.patch.object(vb, 'resolve_image_bytes', side_effect=_resolve):
            out = format_resident_turn_content(
                assembly={'state': '', 'current_day_history': []},
                user_content='图片中央写了什么？',
                is_cold=False,
                is_respawn=False,
                user_image_url='/static/uploads/hot_img.png',
            )
        self.assertIsInstance(out, list)
        self.assertTrue(any(b.get('type') == 'image' for b in out))
        self.assertTrue(any(
            b.get('type') == 'text' and '图片中央' in b.get('text', '')
            for b in out
        ))

    def test_daily_text_only_unchanged(self):
        from chat.daily_runtime import format_resident_turn_content
        out = format_resident_turn_content(
            assembly={'state': '', 'current_day_history': []},
            user_content='hello',
            is_cold=False,
            is_respawn=False,
            user_image_url='',
        )
        self.assertEqual(out, 'hello')


if __name__ == '__main__':
    unittest.main()
