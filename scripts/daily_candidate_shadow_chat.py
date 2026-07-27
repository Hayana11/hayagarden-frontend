#!/usr/bin/env python3
"""Daily Candidate Shadow — 5-turn tone validation.

Pipeline:
  1) POST /api/debug/clean-window/generate-day-handoff  -> /tmp YAML
  2) POST /api/debug/clean-window/start with context_profile=daily_candidate
  3) Run the standard 5 suggested prompts once

Requires (gateway process):
  - CC_CLEAN_WINDOW_SHADOW_ENABLED=1
  - CC_CLEAN_WINDOW_SHADOW_TOKEN in /opt/frontend/.env

Does NOT deploy, does NOT enable on production by default.
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
    token = _load_token()
    if not token:
        raise SystemExit('CC_CLEAN_WINDOW_SHADOW_TOKEN required')
    return {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}


def _post(path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload or {}).encode('utf-8'),
        headers=_headers(),
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise SystemExit('HTTP %s %s\n%s' % (e.code, path, body)) from e


def main() -> int:
    print('Daily Candidate Shadow — base:', BASE)

    handoff = _post('/api/debug/clean-window/generate-day-handoff', {})
    if not handoff.get('ok'):
        print('generate-day-handoff failed:', handoff, file=sys.stderr)
        return 1
    path = handoff['path']
    print('day_handoff:', path)
    print('preview:', json.dumps(handoff.get('preview') or {}, ensure_ascii=False))

    started = _post('/api/debug/clean-window/start', {
        'context_profile': 'daily_candidate',
        'day_handoff_path': path,
    })
    if not started.get('ok'):
        print('start failed:', started, file=sys.stderr)
        return 1

    sid = started['session_id']
    print('session:', sid)
    print('profile:', started.get('context_profile'))
    print('manifest:', json.dumps(started.get('context_manifest') or {}, ensure_ascii=False, indent=2))

    results = []
    try:
        for i, message in enumerate(SUGGESTED, start=1):
            print('\n--- turn %d ---' % i)
            print('you> %s' % message)
            turn = _post('/api/debug/clean-window/turn', {'session_id': sid, 'message': message})
            if not turn.get('ok'):
                print('turn failed:', turn, file=sys.stderr)
                return 1
            content = turn.get('content') or ''
            print('feijia> %s' % content)
            manifest = turn.get('context_manifest') or {}
            results.append({
                'turn': i,
                'user': message,
                'assistant': content,
                'state_injected': manifest.get('state_injected'),
                'state_mode': manifest.get('state_mode'),
                'day_handoff_injected': manifest.get('day_handoff_injected'),
            })
    finally:
        try:
            _post('/api/debug/clean-window/close', {'session_id': sid})
        except SystemExit:
            pass

    out_path = os.environ.get('DAILY_CANDIDATE_RESULT', '/tmp/daily_candidate_5turn.json')
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump({
            'session_id': sid,
            'day_handoff_path': path,
            'turns': results,
        }, fh, ensure_ascii=False, indent=2)
    print('\nWrote', out_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
