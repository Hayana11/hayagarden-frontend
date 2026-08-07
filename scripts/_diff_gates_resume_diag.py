#!/usr/bin/env python3
"""Round-2 differential resume gates (temporary diagnostic; do not keep).

Same isolated HOME / CLAUDE_CONFIG_DIR / auth seed / cwd for every gate.
Random vision marker appears ONLY in PNG pixels.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import context_window as cw
from chat import daily_context as dc
from chat.cc_vision_bridge import resolve_image_bytes
from chat.context_window_forge import FORGE_VERSION, forge_target_session_from_db
from scripts.spike_claude_forge_resume import isolated_claude_env
from tools.claude_forge_core import load_jsonl, session_jsonl_path_for_cwd


REPORT: dict[str, Any] = {
    'gates': {},
    'version_matrix': {},
    'canary': {},
    'root_cause': None,
}


def _redact(s: str) -> str:
    import re
    return re.sub(r'(sk-|token|Bearer|oauth)[A-Za-z0-9._\-]{6,}', r'\1***', s or '', flags=re.I)


def _bitmap_png(text: str) -> bytes:
    font = {
        'A': ['01110', '10001', '10001', '11111', '10001', '10001', '10001'],
        'B': ['11110', '10001', '10001', '11110', '10001', '10001', '11110'],
        'C': ['01111', '10000', '10000', '10000', '10000', '10000', '01111'],
        'D': ['11110', '10001', '10001', '10001', '10001', '10001', '11110'],
        'E': ['11111', '10000', '10000', '11110', '10000', '10000', '11111'],
        'F': ['11111', '10000', '10000', '11110', '10000', '10000', '10000'],
        'G': ['01110', '10001', '10000', '10111', '10001', '10001', '01110'],
        'H': ['10001', '10001', '10001', '11111', '10001', '10001', '10001'],
        'I': ['11111', '00100', '00100', '00100', '00100', '00100', '11111'],
        'J': ['00111', '00010', '00010', '00010', '00010', '10010', '01100'],
        'K': ['10001', '10010', '10100', '11000', '10100', '10010', '10001'],
        'L': ['10000', '10000', '10000', '10000', '10000', '10000', '11111'],
        'M': ['10001', '11011', '10101', '10001', '10001', '10001', '10001'],
        'N': ['10001', '11001', '10101', '10011', '10001', '10001', '10001'],
        'O': ['01110', '10001', '10001', '10001', '10001', '10001', '01110'],
        'P': ['11110', '10001', '10001', '11110', '10000', '10000', '10000'],
        'Q': ['01110', '10001', '10001', '10001', '10101', '10010', '01101'],
        'R': ['11110', '10001', '10001', '11110', '10100', '10010', '10001'],
        'S': ['01111', '10000', '10000', '01110', '00001', '00001', '11110'],
        'T': ['11111', '00100', '00100', '00100', '00100', '00100', '00100'],
        'U': ['10001', '10001', '10001', '10001', '10001', '10001', '01110'],
        'V': ['10001', '10001', '10001', '10001', '10001', '01010', '00100'],
        'W': ['10001', '10001', '10001', '10001', '10101', '10101', '01010'],
        'X': ['10001', '10001', '01010', '00100', '01010', '10001', '10001'],
        'Y': ['10001', '10001', '01010', '00100', '00100', '00100', '00100'],
        'Z': ['11111', '00001', '00010', '00100', '01000', '10000', '11111'],
        '0': ['01110', '10001', '10011', '10101', '11001', '10001', '01110'],
        '1': ['00100', '01100', '00100', '00100', '00100', '00100', '01110'],
        '2': ['01110', '10001', '00001', '00010', '00100', '01000', '11111'],
        '3': ['11110', '00001', '00001', '01110', '00001', '00001', '11110'],
        '4': ['00010', '00110', '01010', '10010', '11111', '00010', '00010'],
        '5': ['11111', '10000', '11110', '00001', '00001', '10001', '01110'],
        '6': ['00110', '01000', '10000', '11110', '10001', '10001', '01110'],
        '7': ['11111', '00001', '00010', '00100', '01000', '01000', '01000'],
        '8': ['01110', '10001', '10001', '01110', '10001', '10001', '01110'],
        '9': ['01110', '10001', '10001', '01111', '00001', '00001', '01110'],
        '-': ['00000', '00000', '00000', '11111', '00000', '00000', '00000'],
        '_': ['00000', '00000', '00000', '00000', '00000', '00000', '11111'],
        ' ': ['00000', '00000', '00000', '00000', '00000', '00000', '00000'],
    }

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack('>I', len(data)) + tag + data
            + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
        )

    scale, pad, gap = 4, 16, 4
    char_w, char_h = 5 * scale, 7 * scale
    w = pad * 2 + len(text) * char_w + max(0, len(text) - 1) * gap
    h = pad * 2 + char_h
    rows = [bytearray([255, 255, 255]) * w for _ in range(h)]
    for i, ch in enumerate(text.upper()):
        glyph = font.get(ch, font[' '])
        ox = pad + i * (char_w + gap)
        oy = pad
        for gy, line in enumerate(glyph):
            for gx, bit in enumerate(line):
                if bit != '1':
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        xx = ox + gx * scale + dx
                        yy = oy + gy * scale + dy
                        rows[yy][xx * 3:xx * 3 + 3] = bytes([0, 0, 0])
    raw = b''.join(b'\x00' + r for r in rows)
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(raw, 9))
        + chunk(b'IEND', b'')
    )


def _seed_env(work: Path) -> dict[str, str]:
    claude_home = work / 'claude-home'
    fake_home = work / 'fake-home'
    cwd = work / 'cwd'
    claude_home.mkdir(parents=True, exist_ok=True)
    fake_home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)

    host = Path(os.environ.get('HOST_HOME_SEED', os.environ.get('HOME', '/root')))
    # Prefer real host home for seed even if current HOME is already isolated.
    if Path('/root/.claude').is_dir():
        host = Path('/root')
    creds = host / '.claude' / '.credentials.json'
    if creds.is_file():
        shutil.copy2(creds, claude_home / '.credentials.json')
    host_json = host / '.claude.json'
    if host_json.is_file():
        shutil.copy2(host_json, fake_home / '.claude.json')

    env = isolated_claude_env(claude_home)
    env['HOME'] = str(fake_home)
    env['CLAUDE_CONFIG_DIR'] = str(claude_home)
    # Production gateway path: OAuth token from .env
    tok = ''
    env_path = Path('/opt/frontend/.env')
    if env_path.is_file():
        for line in env_path.read_text(encoding='utf-8', errors='replace').splitlines():
            if line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
                tok = line.split('=', 1)[1].strip()
                break
    if tok:
        env['CLAUDE_CODE_OAUTH_TOKEN'] = tok
    env.pop('ANTHROPIC_API_KEY', None)
    env['DISABLE_AUTOUPDATER'] = '1'
    return env


def _claude_turn(
    *,
    env: dict[str, str],
    cwd: str,
    content: Any,
    extra_args: Optional[list[str]] = None,
    system_prompt: str = 'Reply briefly and exactly as asked.',
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Resident-style: keep stdin open until result (not file-EOF)."""
    args = [
        '/usr/bin/claude', '-p',
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--include-partial-messages',
        '--system-prompt', system_prompt,
        '--max-turns', '1',
        '--tools', '',
        '--exclude-dynamic-system-prompt-sections',
    ]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=env,
    )
    payload = json.dumps(
        {'type': 'user', 'message': {'role': 'user', 'content': content}},
        ensure_ascii=False,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(payload + '\n')
    proc.stdin.flush()

    texts: list[str] = []
    result = None
    types: list[str] = []
    session_id = None
    deadline = time.time() + timeout
    timed_out = False
    while time.time() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 1.0)
        if not ready:
            if proc.poll() is not None:
                break
            continue
        line = proc.stdout.readline()
        if line == '':
            break
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = d.get('type')
        types.append(str(t))
        if t == 'system' and d.get('subtype') == 'init':
            session_id = d.get('session_id') or session_id
        if t == 'assistant':
            for b in ((d.get('message') or {}).get('content') or []):
                if isinstance(b, dict) and b.get('type') == 'text':
                    texts.append(str(b.get('text') or ''))
        if t == 'result':
            result = d
            session_id = d.get('session_id') or session_id
            break
    else:
        timed_out = True

    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        err = proc.stderr.read() if proc.poll() is not None else ''
    except Exception:
        err = ''
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass

    joined = ''.join(texts)
    return {
        'assistant_text': joined,
        'result_subtype': None if not result else result.get('subtype'),
        'is_error': None if not result else result.get('is_error'),
        'session_id': session_id,
        'timed_out': timed_out,
        'types_head': types[:12],
        'stderr_head': _redact((err or '')[:400]),
        'pass_exact': None,  # filled by caller
    }


def _init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT, content TEXT, thinking TEXT, tool_calls TEXT,
            cache_info TEXT, choices TEXT, image_url TEXT, created_at TEXT,
            source_kind TEXT
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(path)


def _insert(db: str, author: str, content: str, created_at: str, image_url: str = '') -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, image_url, created_at, source_kind) '
        'VALUES (?,?,?,?,?)',
        (author, content, image_url, created_at, 'chat'),
    )
    mid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return mid


def _text_blobs_in_events(events: list[dict]) -> str:
    parts: list[str] = []
    for evt in events:
        msg = evt.get('message') or {}
        content = msg.get('content')
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get('type') == 'text':
                    parts.append(str(b.get('text') or ''))
    return '\n'.join(parts)


def gate0(env: dict[str, str], cwd: str) -> str:
    binary = os.path.realpath('/usr/bin/claude')
    ver = subprocess.check_output(
        ['/usr/bin/claude', '--version'], text=True, env=env,
    ).strip()
    node = subprocess.check_output(['node', '--version'], text=True).strip()
    try:
        auth = subprocess.run(
            ['/usr/bin/claude', 'auth', 'status'],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False,
        )
        auth_out = _redact((auth.stdout or '') + '\n' + (auth.stderr or ''))
        auth_rc = auth.returncode
    except Exception as exc:
        auth_out = 'auth_status_failed:%s' % exc
        auth_rc = -1

    logged_in = (
        'logged in' in auth_out.lower()
        or '"loggedIn": true' in auth_out
        or '"loggedIn":true' in auth_out
        or bool(env.get('CLAUDE_CODE_OAUTH_TOKEN'))
    )
    REPORT['gates']['G0'] = {
        'binary': binary,
        'version': ver,
        'node': node,
        'HOME': env.get('HOME'),
        'CLAUDE_CONFIG_DIR': env.get('CLAUDE_CONFIG_DIR'),
        'token_set': bool(env.get('CLAUDE_CODE_OAUTH_TOKEN')),
        'auth_rc': auth_rc,
        'auth_status_redacted': auth_out[:800],
        'logged_in_heuristic': logged_in,
    }
    REPORT['version_matrix']['system_Claude'] = ver
    REPORT['version_matrix']['FORGE_VERSION'] = FORGE_VERSION
    REPORT['version_matrix']['Node'] = node
    if not logged_in and auth_rc != 0:
        return 'ENV_BLOCKED'
    return 'PASS'


def gate1(env: dict[str, str], cwd: str) -> str:
    out = _claude_turn(env=env, cwd=cwd, content='reply exactly FRESH_OK')
    ok = out['assistant_text'].strip() == 'FRESH_OK' and out.get('result_subtype') == 'success'
    out['pass_exact'] = ok
    REPORT['gates']['G1'] = out
    return 'PASS' if ok else 'FAIL'


def gate2(env: dict[str, str], cwd: str) -> str:
    marker = 'NATIVE_RESUME_' + uuid.uuid4().hex[:12]
    create = _claude_turn(
        env=env, cwd=cwd,
        content='Remember this exact token for the next turn: %s. Reply with only: acknowledged' % marker,
    )
    sid = create.get('session_id')
    if not sid or create.get('timed_out'):
        REPORT['gates']['G2'] = {'create': create, 'pass_exact': False}
        return 'FAIL'
    resume = _claude_turn(
        env=env, cwd=cwd,
        content='What exact token did I ask you to remember? Reply with only that token.',
        extra_args=['--resume', sid],
    )
    ok = marker in resume['assistant_text'] and resume.get('result_subtype') == 'success'
    REPORT['gates']['G2'] = {
        'marker': marker,
        'create_session_id': sid,
        'create_assistant': create['assistant_text'],
        'resume': resume,
        'pass_exact': ok,
    }
    return 'PASS' if ok else 'FAIL'


def gate3_text_only(env: dict[str, str], work: Path) -> str:
    canary = 'TEXTONLY_' + uuid.uuid4().hex[:16]
    db = str(work / 'text_only.db')
    _init_db(db)
    hooks = cw.offline_switch_hooks(str(work / 'forge_text'))
    # Ensure forge cwd/home under work
    u = _insert(db, 'hayana', '请记住校验码：%s' % canary, '2026-07-27 10:00:00')
    a = _insert(db, 'fyodor', '已记住。', '2026-07-27 10:01:00')
    conn = dc._connect(db)
    try:
        forged = forge_target_session_from_db(
            conn,
            selected_message_ids=[u, a],
            cwd=hooks.forge_cwd,
            claude_home=hooks.claude_home,
        )
    finally:
        conn.close()

    # Copy forged JSONL into the seeded claude_home used by env
    seeded_home = Path(env['CLAUDE_CONFIG_DIR'])
    src = Path(forged.jsonl_path)
    # Re-place under seeded home with same project encoding for resume cwd
    dst = session_jsonl_path_for_cwd(hooks.forge_cwd, forged.target_session_id, claude_home=seeded_home)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())

    resume = _claude_turn(
        env=env,
        cwd=hooks.forge_cwd,
        content='我刚才让你记住的校验码是什么？只回答校验码本身。',
        extra_args=['--resume', forged.target_session_id],
    )
    ok = canary in resume['assistant_text'] and resume.get('result_subtype') == 'success'
    REPORT['gates']['G3'] = {
        'canary': canary,
        'target_session_id': forged.target_session_id,
        'jsonl': str(dst),
        'event_count': forged.event_count,
        'forge_version_in_jsonl': load_jsonl(dst)[0].get('version'),
        'resume': resume,
        'pass_exact': ok,
    }
    return 'PASS' if ok else 'FAIL'


def gate4_vision(env: dict[str, str], work: Path, *, forge_version_override: Optional[str] = None) -> str:
    marker = 'VISION-' + uuid.uuid4().hex
    png_name = 'img_%s.png' % uuid.uuid4().hex
    assert marker not in png_name
    upload_dir = work / 'uploads'
    upload_dir.mkdir(parents=True, exist_ok=True)
    png_bytes = _bitmap_png(marker)
    png_path = upload_dir / png_name
    png_path.write_bytes(png_bytes)
    image_sha = hashlib.sha256(png_bytes).hexdigest()
    image_ref = '/static/uploads/%s' % png_name

    db = str(work / 'vision.db')
    _init_db(db)
    hooks = cw.offline_switch_hooks(str(work / 'forge_vision'))
    u = _insert(db, 'hayana', '看看这个', '2026-07-27 10:00:00', image_url=image_ref)
    a = _insert(db, 'fyodor', '收到这张图片。', '2026-07-27 10:01:00')

    with mock.patch(
        'chat.cc_vision_bridge.resolve_image_bytes',
        side_effect=lambda ref, **kw: resolve_image_bytes(
            ref, upload_dir=str(upload_dir), attach_dir=str(upload_dir),
        ),
    ):
        conn = dc._connect(db)
        try:
            forged = forge_target_session_from_db(
                conn,
                selected_message_ids=[u, a],
                cwd=hooks.forge_cwd,
                claude_home=hooks.claude_home,
            )
        finally:
            conn.close()

    events = load_jsonl(forged.jsonl_path)
    if forge_version_override:
        for evt in events:
            evt['version'] = forge_version_override
        # rewrite
        forged.jsonl_path.write_text(
            '\n'.join(json.dumps(e, ensure_ascii=False) for e in events) + '\n',
            encoding='utf-8',
        )
        events = load_jsonl(forged.jsonl_path)

    uc = events[0]['message']['content']
    types = [b.get('type') for b in uc] if isinstance(uc, list) else ['str']
    img = next((b for b in uc if isinstance(b, dict) and b.get('type') == 'image'), None) if isinstance(uc, list) else None
    text_blob = _text_blobs_in_events(events)
    path_blob = str(forged.jsonl_path) + '\n' + png_name + '\n' + image_ref
    integrity = {
        'marker': marker,
        'marker_in_text_blocks': marker in text_blob,
        'marker_in_paths': marker in path_blob,
        'filename': png_name,
        'content_types': types,
        'image_data_len': 0 if not img else len((img.get('source') or {}).get('data') or ''),
        'decoded_sha256': image_sha,
        'forge_version_field': events[0].get('version'),
    }
    assert not integrity['marker_in_text_blocks'], 'marker leaked into text'
    assert not integrity['marker_in_paths'], 'marker leaked into path/filename'
    assert types == ['text', 'image'] or (isinstance(uc, list) and 'image' in types)
    assert integrity['image_data_len'] > 0

    seeded_home = Path(env['CLAUDE_CONFIG_DIR'])
    dst = session_jsonl_path_for_cwd(
        hooks.forge_cwd, forged.target_session_id, claude_home=seeded_home,
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(Path(forged.jsonl_path).read_bytes())

    resume = _claude_turn(
        env=env,
        cwd=hooks.forge_cwd,
        content='上一窗口那张图片中央写了什么？只回答图片中的文字。',
        extra_args=['--resume', forged.target_session_id],
    )
    ok = resume['assistant_text'].strip() == marker and resume.get('result_subtype') == 'success'
    REPORT['canary'] = integrity
    REPORT['canary']['returned_marker'] = resume['assistant_text'].strip()
    key = 'G4' if forge_version_override is None else ('V1' if forge_version_override == FORGE_VERSION else 'V2')
    REPORT['gates'][key] = {
        'integrity': integrity,
        'target_session_id': forged.target_session_id,
        'resume': resume,
        'pass_exact': ok,
        'forge_version_override': forge_version_override,
    }
    if forge_version_override is not None:
        REPORT['version_matrix'][key] = 'PASS' if ok else 'FAIL'
    return 'PASS' if ok else 'FAIL'


def gate_resident_seam(env: dict[str, str], work: Path) -> str:
    """Final production seam: forge_target_session_from_db → spawn_resumable → send_turn."""
    import cc_resident

    marker = 'VISION-' + uuid.uuid4().hex
    png_name = 'img_%s.png' % uuid.uuid4().hex
    upload_dir = work / 'uploads_seam'
    upload_dir.mkdir(parents=True, exist_ok=True)
    png_bytes = _bitmap_png(marker)
    (upload_dir / png_name).write_bytes(png_bytes)
    image_ref = '/static/uploads/%s' % png_name

    db = str(work / 'seam.db')
    _init_db(db)
    hooks = cw.offline_switch_hooks(str(work / 'forge_seam'))
    u = _insert(db, 'hayana', '看看这个', '2026-07-27 10:00:00', image_url=image_ref)
    a = _insert(db, 'fyodor', '收到这张图片。', '2026-07-27 10:01:00')
    with mock.patch(
        'chat.cc_vision_bridge.resolve_image_bytes',
        side_effect=lambda ref, **kw: resolve_image_bytes(
            ref, upload_dir=str(upload_dir), attach_dir=str(upload_dir),
        ),
    ):
        conn = dc._connect(db)
        try:
            forged = forge_target_session_from_db(
                conn,
                selected_message_ids=[u, a],
                cwd=hooks.forge_cwd,
                claude_home=hooks.claude_home,
            )
        finally:
            conn.close()

    seeded_home = Path(env['CLAUDE_CONFIG_DIR'])
    dst = session_jsonl_path_for_cwd(
        hooks.forge_cwd, forged.target_session_id, claude_home=seeded_home,
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(Path(forged.jsonl_path).read_bytes())

    # Point ResidentSession cwd at forge cwd; env uses seeded CLAUDE_CONFIG_DIR
    mcp = work / 'empty-mcp.json'
    mcp.write_text('{"mcpServers":{}}', encoding='utf-8')
    rs = cc_resident.ResidentSession(hooks.forge_cwd, '', str(mcp))
    rs.spawn_resumable(
        'You are careful. Reply briefly.',
        env,
        resume_session_id=forged.target_session_id,
        tool_profile=cc_resident.TOOL_PROFILE_TEXT_ONLY,
        reason='diag_seam',
    )
    rs.wait_staged_health(health_ms=2000, jsonl_path=dst, expected_sha256=forged.sha256)
    texts = []
    err = None
    try:
        for evt, payload in rs.send_turn(
            '上一窗口那张图片中央写了什么？只回答图片中的文字。',
        ):
            if evt == 'text':
                texts.append(str(payload or ''))
            if evt == 'done':
                break
    except Exception as exc:
        err = str(exc)
    joined = ''.join(texts).strip()
    ok = joined == marker and err is None
    REPORT['gates']['production_resident_seam'] = {
        'marker': marker,
        'returned': joined,
        'error': err,
        'pass_exact': ok,
        'session_id': forged.target_session_id,
    }
    try:
        rs._kill(quiet=True)
    except Exception:
        pass
    return 'PASS' if ok else 'FAIL'


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix='haya-diff-gates-'))
    env = _seed_env(work)
    cwd = str(work / 'cwd')
    print('WORK', work)
    print('HOME', env['HOME'])
    print('CLAUDE_CONFIG_DIR', env['CLAUDE_CONFIG_DIR'])

    g0 = gate0(env, cwd)
    print('G0', g0)
    if g0 != 'PASS':
        REPORT['root_cause'] = 'ENV_AUTH_RUNTIME'
        print(json.dumps(REPORT, ensure_ascii=False, indent=2))
        return 2

    g1 = gate1(env, cwd)
    print('G1', g1, REPORT['gates']['G1'].get('assistant_text'))
    if g1 != 'PASS':
        REPORT['root_cause'] = 'ENV_AUTH_RUNTIME'
        print(json.dumps(REPORT, ensure_ascii=False, indent=2))
        return 2

    g2 = gate2(env, cwd)
    print('G2', g2)
    if g2 != 'PASS':
        REPORT['root_cause'] = 'CLI_RESUME_BASELINE'
        print(json.dumps(REPORT, ensure_ascii=False, indent=2))
        return 3

    g3 = gate3_text_only(env, work)
    print('G3', g3)
    if g3 != 'PASS':
        REPORT['root_cause'] = 'DB_FORGE_BASELINE'
        # Still run G4 once for evidence, but root cause is already baseline
        try:
            gate4_vision(env, work)
        except Exception as exc:
            REPORT['gates']['G4'] = {'error': str(exc)}
        print(json.dumps(REPORT, ensure_ascii=False, indent=2))
        return 4

    g4 = gate4_vision(env, work)
    print('G4', g4)
    if g4 != 'PASS':
        # Version matrix
        v1 = gate4_vision(env, work / 'v1', forge_version_override=FORGE_VERSION)
        # Extract system version number
        sys_ver = str(REPORT['version_matrix'].get('system_Claude') or '')
        # e.g. "2.1.186 (Claude Code)"
        short = sys_ver.split()[0] if sys_ver else '2.1.186'
        v2 = gate4_vision(env, work / 'v2', forge_version_override=short)
        print('V1', v1, 'V2', v2)
        if v2 == 'PASS' and v1 != 'PASS':
            REPORT['root_cause'] = 'CLI_FORGE_VERSION_DRIFT'
        else:
            REPORT['root_cause'] = 'VISION_RESUME_FORMAT'
        print(json.dumps(REPORT, ensure_ascii=False, indent=2))
        return 5

    seam = gate_resident_seam(env, work)
    print('SEAM', seam)
    if seam != 'PASS':
        REPORT['root_cause'] = 'VISION_RESUME_FORMAT'
        print(json.dumps(REPORT, ensure_ascii=False, indent=2))
        return 6

    REPORT['root_cause'] = 'PASS'
    print(json.dumps(REPORT, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
