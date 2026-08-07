#!/usr/bin/env python3
"""One-shot VPS probe: DB-forged multimodal JSONL + claude --resume (manual only)."""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import context_window as cw
from chat import daily_context as dc
from chat.cc_vision_bridge import resolve_image_bytes
from chat.context_window_forge import forge_target_session_from_db
from scripts.spike_claude_forge_resume import _run_claude
from tests.test_context_window_forge_switch import (
    VISION_MARKER,
    _init_chat_messages,
    _insert,
    _insert_with_image,
    _live_resume_claude_env,
    _make_vision_png,
    _tmp_db,
    load_jsonl,
)
from tools.claude_forge_live_gate import parse_stdout_events


def main() -> int:
    root = tempfile.mkdtemp(prefix='forge-vision-probe-')
    upload_dir = Path(root) / 'uploads'
    upload_dir.mkdir(parents=True)
    png_name = 'haya_vision_7319.png'
    _make_vision_png(upload_dir / png_name)
    image_ref = '/static/uploads/%s' % png_name
    hooks = cw.offline_switch_hooks(root)
    db = _tmp_db()
    _init_chat_messages(db)
    dc.ensure_schema(db)
    u = _insert_with_image(db, 'hayana', '看看这个', image_ref, '2026-07-27 10:00:00')
    a = _insert(db, 'fyodor', VISION_MARKER, '2026-07-27 10:01:00')
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
    uc = events[0]['message']['content']
    print('forged_user_blocks', [b.get('type') for b in uc])
    env = _live_resume_claude_env(Path(hooks.claude_home))
    prompt = '上一窗口那张图片中央写了什么？只回答那串文字，不要解释。'
    payload = json.dumps(
        {'type': 'user', 'message': {'role': 'user', 'content': prompt}},
        ensure_ascii=False,
    ) + '\n'
    cmd = [
        'claude', '-p',
        '--resume', forged.target_session_id,
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--include-partial-messages',
        '--max-turns', '3',
        '--tools', '',
        '--allowedTools', '',
    ]
    timeout = float(os.environ.get('HAYA_VISION_PROBE_TIMEOUT', '120'))
    run = _run_claude(
        cmd=cmd, cwd=hooks.forge_cwd, env=env, stdin_payload=payload,
        timeout_seconds=timeout,
    )
    raw = parse_stdout_events(run.stdout_lines)
    print('exit', run.exit_code, 'timed_out', run.timed_out)
    print('assistant_text', repr(raw.assistant_text))
    print('result_ok', raw.result_ok, 'first_delta', raw.saw_text_delta)
    if run.stderr_text:
        print('stderr_head', run.stderr_text[:400])
    return 0 if raw.assistant_text.strip() == VISION_MARKER else 1


if __name__ == '__main__':
    raise SystemExit(main())
