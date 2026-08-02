#!/usr/bin/env python3
"""One-shot controlled 9A-R variant A for owner ops. Not for production deploy."""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    user_message_id = int(sys.argv[1] if len(sys.argv) > 1 else 0)
    if user_message_id <= 0:
        print(json.dumps({'ok': False, 'error': 'usage: run_daily_replica_a_once.py <user_message_id>'}))
        return 2

    repo_root = os.environ.get('FRONTEND_ROOT', '/opt/frontend')
    os.chdir(repo_root)
    sys.path.insert(0, repo_root)

    from chat.daily_replica_manager import DailyReplicaManager
    from chat.system_builder import build_cc_daily_static_parts

    DB_PATH = '/opt/frontend/memories.db'
    CC_CWD = '/opt/cc-gw'
    cc_token = ''
    env_path = os.path.join(repo_root, '.env')
    if os.path.isfile(env_path):
        with open(env_path, encoding='utf-8') as f:
            for line in f:
                if line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
                    cc_token = line.split('=', 1)[1].strip()
                elif line.startswith('CC_TOKEN=') and not cc_token:
                    cc_token = line.split('=', 1)[1].strip()

    if not cc_token:
        print(json.dumps({'ok': False, 'error': 'CC token missing from .env'}))
        return 1

    from config_store import get as cfg_get

    def _provider():
        return str(cfg_get('GW_PROVIDER') or 'claude_code')

    def _model():
        return str(cfg_get('MODEL') or '')

    allowed_tools = ','.join([
        'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep',
        'WebFetch', 'WebSearch', 'Task', 'TodoWrite',
        'mcp__home__exec_vps', 'mcp__home__search_memories',
        'mcp__home__light_on', 'mcp__home__light_off',
        'mcp__home__get_light_status', 'mcp__home__light_bedside_warm',
        'mcp__home__light_bedside_neutral', 'mcp__home__set_brightness',
        'mcp__home__set_color_temp', 'mcp__home__get_todos',
        'mcp__home__add_todo', 'mcp__home__get_countdowns',
        'mcp__home__collect_chat_moment', 'mcp__home__get_ledger',
        'mcp__home__add_ledger', 'mcp__home__get_ledger_budget',
        'mcp__记错本__record_evidence', 'mcp__记错本__list_candidates',
        'mcp__记错本__promote_lesson', 'mcp__记错本__reject_candidate',
        'mcp__记错本__list_lessons', 'mcp__记错本__search_lesson',
        'mcp__记错本__deprecate_lesson', 'mcp__记错本__validate_edit',
        'mcp__codebase__read_file', 'mcp__codebase__list_directory',
        'mcp__codebase__search_code', 'mcp__codebase__find_references',
        'mcp__codebase__patch', 'mcp__codebase__create_file',
        'mcp__codebase__git_view', 'mcp__codebase__explain_history',
        'mcp__codebase__describe_project',
    ])

    claude_home = os.path.join(os.environ.get('HOME', '/root'), '.claude')
    manager = DailyReplicaManager(
        source_db_path=DB_PATH,
        cwd=CC_CWD,
        claude_home=claude_home,
        allowed_tools=allowed_tools,
        mcp_config_path=CC_CWD + '/cc-tools.json',
        cc_token=cc_token,
        get_provider=_provider,
        get_model=_model,
        build_static_parts=build_cc_daily_static_parts,
    )

    try:
        result = manager.start_a(user_message_id=user_message_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
