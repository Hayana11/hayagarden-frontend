#!/usr/bin/env python3
"""Daily Candidate Shadow — inspect or run the 5-turn tone validation.

Step 1 (human review):
  python3 scripts/generate_day_handoff.py > /tmp/review.yaml
  # edit/review the YAML, place final file under /tmp/hayagarden-clean-shadow/

Step 2 (optional check only):
  python3 scripts/daily_candidate_shadow_chat.py --handoff-path /tmp/hayagarden-clean-shadow/day_handoff_20260726.yaml

Step 3 (explicit 5 model calls):
  python3 scripts/daily_candidate_shadow_chat.py --handoff-path ... --run

Requires gateway:
  - CC_CLEAN_WINDOW_SHADOW_ENABLED=1
  - CC_CLEAN_WINDOW_SHADOW_TOKEN in /opt/frontend/.env

This script never generates handoff by itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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


def _inspect_handoff(path: str) -> tuple[str, str]:
    from chat.day_handoff import format_day_handoff_yaml, load_and_validate_day_handoff

    data, errors = load_and_validate_day_handoff(path)
    if errors:
        raise SystemExit('handoff validation failed: ' + '; '.join(errors))
    yaml_text = format_day_handoff_yaml(data)
    sha = hashlib.sha256(yaml_text.encode('utf-8')).hexdigest()
    print('Validated handoff:', path)
    print('yaml_sha256:', sha)
    print('source_sha256:', data.get('source_sha256'))
    print('source_day:', data.get('source_day'))
    print('source_start_at:', data.get('source_start_at'))
    print('source_end_at:', data.get('source_end_at'))
    print('requires_human_review:', data.get('requires_human_review'))
    print('\n--- handoff yaml ---\n')
    print(yaml_text, end='')
    return yaml_text, sha


def main() -> int:
    parser = argparse.ArgumentParser(description='Daily Candidate Shadow inspect/run helper')
    parser.add_argument('--handoff-path', required=True, help='Validated YAML under /tmp/hayagarden-clean-shadow/')
    parser.add_argument('--run', action='store_true', help='Execute 5 model calls (default: inspect only)')
    args = parser.parse_args()

    print('Daily Candidate Shadow — base:', BASE)
    _inspect_handoff(args.handoff_path)

    if not args.run:
        print('\nInspect only. Re-run with --run to execute 5 model calls.', file=sys.stderr)
        return 0

    print('\n--- starting shadow session (5 model calls) ---', file=sys.stderr)
    started = _post('/api/debug/clean-window/start', {
        'context_profile': 'daily_candidate',
        'day_handoff_path': args.handoff_path,
    })
    if not started.get('ok'):
        print('start failed:', started, file=sys.stderr)
        return 1

    sid = started['session_id']
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
                'day_handoff_loaded': manifest.get('day_handoff_loaded'),
                'day_handoff_injected_this_turn': manifest.get('day_handoff_injected_this_turn'),
                'state_injected_this_turn': manifest.get('state_injected_this_turn'),
                'state_mode': manifest.get('state_mode'),
            })
    finally:
        try:
            _post('/api/debug/clean-window/close', {'session_id': sid})
        except SystemExit:
            pass

    out_path = os.environ.get('DAILY_CANDIDATE_RESULT', '/tmp/daily_candidate_5turn.json')
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump({'session_id': sid, 'day_handoff_path': args.handoff_path, 'turns': results}, fh,
                  ensure_ascii=False, indent=2)
    print('\nWrote', out_path, file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
