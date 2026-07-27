#!/usr/bin/env python3
"""Interactive helper for P-CONTEXT-CLEAN-WINDOW-SHADOW tone A/B.

Requires on the gateway process:
  - CC_CLEAN_WINDOW_SHADOW_ENABLED=1 in runtime_config
  - CC_CLEAN_WINDOW_SHADOW_TOKEN in /opt/frontend/.env

Token is read when frontend-gw starts (not per-request like runtime_config).
Write the token to .env before deploying or restarting frontend-gw.

Default gateway base: http://127.0.0.1:5051

Example:
  cd /opt/frontend
  python3 scripts/clean_window_shadow_chat.py

Suggested 5-turn prompt set (compare with formal /chat window):
  1. 爸爸，我今天有一点累，你抱着小猫说一会儿话。
  2. 不许给我列建议，只要陪我。
  3. 嗯，再哄一点。
  4. 你怎么突然不说话了？
  5. 那爸爸现在想对小猫说什么？
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get('CLEAN_WINDOW_GW_BASE', 'http://127.0.0.1:5051').rstrip('/')
ENV_PATH = os.environ.get('HAYAGARDEN_ENV_PATH', '/opt/frontend/.env')
SUGGESTED = [
    '爸爸，我今天有一点累，你抱着小猫说一会儿话。',
    '不许给我列建议，只要陪我。',
    '嗯，再哄一点。',
    '你怎么突然不说话了？',
    '那爸爸现在想对小猫说什么？',
]


def _load_token() -> str:
    token = os.environ.get('CC_CLEAN_WINDOW_SHADOW_TOKEN', '').strip()
    if token:
        return token
    try:
        for line in open(ENV_PATH, encoding='utf-8'):
            if line.startswith('CC_CLEAN_WINDOW_SHADOW_TOKEN='):
                return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return ''


def _headers() -> dict[str, str]:
    headers = {'Content-Type': 'application/json'}
    token = _load_token()
    if not token:
        raise SystemExit(
            'CC_CLEAN_WINDOW_SHADOW_TOKEN is required (set in env or %s)' % ENV_PATH
        )
    headers['Authorization'] = 'Bearer ' + token
    return headers


def _post(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers=_headers(),
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise SystemExit('HTTP %s %s\n%s' % (e.code, path, body)) from e


def main() -> int:
    print('Clean Window Shadow chat — base:', BASE)
    started = _post('/api/debug/clean-window/start')
    if not started.get('ok'):
        print('start failed:', started, file=sys.stderr)
        return 1
    sid = started['session_id']
    print('session:', sid)
    print('static_system_sha256:', started.get('static_system_sha256'))
    print('manifest:', json.dumps(started.get('context_manifest') or {}, ensure_ascii=False, indent=2))
    print('\nEnter messages (blank line uses suggested prompts in order). Ctrl-D to exit.\n')

    suggested_idx = 0
    try:
        while True:
            prompt = 'you> ' if suggested_idx >= len(SUGGESTED) else 'you[%d]> ' % (suggested_idx + 1)
            try:
                line = input(prompt)
            except EOFError:
                break
            if not line.strip():
                if suggested_idx < len(SUGGESTED):
                    line = SUGGESTED[suggested_idx]
                    suggested_idx += 1
                    print(line)
                else:
                    continue
            turn = _post('/api/debug/clean-window/turn', {'session_id': sid, 'message': line})
            if not turn.get('ok'):
                print('turn failed:', turn, file=sys.stderr)
                return 1
            print('feijia> %s' % (turn.get('content') or ''))
            manifest = turn.get('context_manifest') or {}
            print(
                '[turn=%s history=%s sha=%s save_suppressed=%s executor=%s]'
                % (
                    turn.get('turn_index'),
                    turn.get('history_message_count'),
                    (turn.get('static_system_sha256') or '')[:12],
                    manifest.get('save_marker_suppressed'),
                    manifest.get('actual_executor'),
                )
            )
    finally:
        try:
            closed = _post('/api/debug/clean-window/close', {'session_id': sid})
            print('\nclosed:', closed.get('session_id'))
        except SystemExit:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
