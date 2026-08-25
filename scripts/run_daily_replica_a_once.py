#!/usr/bin/env python3
"""Interactive 9A-R A→B pairing in one process. Not for production deploy."""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Optional


def _flush_print(*args: Any, **kwargs: Any) -> None:
    print(*args, **kwargs)
    sys.stdout.flush()


def _print_hashes(label: str, snapshot_manifest: dict[str, Any], runner_manifest: dict[str, Any]) -> None:
    _flush_print(f'--- {label} material hashes ---')
    _flush_print('common_material_sha256:', snapshot_manifest.get('common_material_sha256'))
    _flush_print('frozen_session_start_sha256:', snapshot_manifest.get('frozen_session_start_sha256'))
    _flush_print('replica_settings_sha256:', snapshot_manifest.get('replica_settings_sha256'))
    if runner_manifest.get('frozen_session_start_sha256'):
        _flush_print('runner_frozen_session_start_sha256:', runner_manifest.get('frozen_session_start_sha256'))
    if runner_manifest.get('replica_settings_sha256'):
        _flush_print('runner_replica_settings_sha256:', runner_manifest.get('replica_settings_sha256'))


def _print_a_result(result: dict[str, Any]) -> None:
    snapshot_manifest = dict(result.get('snapshot_manifest') or {})
    runner_manifest = dict(result.get('runner_manifest') or {})
    _flush_print('===9A-R-A-BEGIN===')
    _flush_print('experiment_id:', result.get('experiment_id'))
    _flush_print('--- thinking ---')
    _flush_print(result.get('thinking') or '')
    _flush_print('--- content ---')
    _flush_print(result.get('content') or '')
    _flush_print('--- snapshot_manifest ---')
    _flush_print(json.dumps(snapshot_manifest, ensure_ascii=False, indent=2))
    _flush_print('--- runner_manifest ---')
    _flush_print(json.dumps(runner_manifest, ensure_ascii=False, indent=2))
    _print_hashes('A', snapshot_manifest, runner_manifest)
    _flush_print('===9A-R-A-END===')


def _print_b_result(result: dict[str, Any]) -> None:
    runner_manifest = dict(result.get('runner_manifest') or {})
    _flush_print('===9A-R-B-BEGIN===')
    _flush_print('experiment_id:', result.get('experiment_id'))
    _flush_print('--- thinking ---')
    _flush_print(result.get('thinking') or '')
    _flush_print('--- content ---')
    _flush_print(result.get('content') or '')
    _flush_print('--- runner_manifest ---')
    _flush_print(json.dumps(runner_manifest, ensure_ascii=False, indent=2))
    _flush_print('===9A-R-B-END===')


def _close_quietly(manager: Any, experiment_id: str) -> None:
    try:
        manager.close(experiment_id=experiment_id)
        _flush_print('CLOSED:', experiment_id)
    except Exception as exc:
        _flush_print('CLOSE_ERROR:', str(exc))


def _build_manager(repo_root: str) -> Any:
    from chat.daily_replica_manager import DailyReplicaManager
    from chat.system_builder import build_cc_daily_static_parts
    from config_store import get as cfg_get

    DB_PATH = '/opt/frontend/memories.db'
    CC_CWD = '/opt/cc-gw'
    cc_token = ''
    env_path = '/opt/frontend/.env'
    if os.path.isfile(env_path):
        with open(env_path, encoding='utf-8') as f:
            for line in f:
                if line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
                    cc_token = line.split('=', 1)[1].strip()
                elif line.startswith('CC_TOKEN=') and not cc_token:
                    cc_token = line.split('=', 1)[1].strip()

    if not cc_token:
        raise RuntimeError('CC token missing from .env')

    def _provider() -> str:
        return str(cfg_get('GW_PROVIDER') or 'claude_code')

    def _model() -> str:
        return str(cfg_get('MODEL') or '')

    allowed_tools = ','.join([
        'mcp__brain__breath', 'mcp__brain__grow', 'mcp__brain__hold',
        'mcp__brain__pulse', 'mcp__brain__trace',
        'mcp__codebase',
        'mcp__home__light_on', 'mcp__home__light_off', 'mcp__home__get_light_status',
        'mcp__home__light_bedside_warm', 'mcp__home__light_bedside_neutral',
        'mcp__home__get_todos', 'mcp__home__add_todo', 'mcp__home__get_countdowns',
        'mcp__home__get_ledger', 'mcp__home__add_ledger', 'mcp__home__get_ledger_budget',
        'mcp__home__search_memories',
        'mcp__home__collect_chat_moment',
    ])

    claude_home = os.path.join(os.environ.get('HOME', '/root'), '.claude')
    return DailyReplicaManager(
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


def main() -> int:
    user_message_id = int(sys.argv[1] if len(sys.argv) > 1 else 0)
    if user_message_id <= 0:
        _flush_print('usage: run_daily_replica_a_once.py <user_message_id>')
        return 2

    repo_root = os.environ.get('FRONTEND_ROOT', '/opt/frontend')
    os.chdir(repo_root)
    sys.path.insert(0, repo_root)

    manager: Optional[Any] = None
    experiment_id: Optional[str] = None

    try:
        manager = _build_manager(repo_root)
        a_result = manager.start_a(user_message_id=user_message_id)
        experiment_id = str(a_result.get('experiment_id') or '')
        if not experiment_id:
            _flush_print('FATAL: start_a returned no experiment_id')
            return 1
        _print_a_result(a_result)
        _flush_print('AWAITING_COMMAND: CONFIRM_A_REPRODUCED <experiment_id> | CLOSE <experiment_id>')

        while True:
            line = sys.stdin.readline()
            if not line:
                _flush_print('EOF: closing experiment')
                if experiment_id:
                    _close_quietly(manager, experiment_id)
                return 0

            cmd = line.strip()
            if not cmd:
                continue

            if cmd == f'CONFIRM_A_REPRODUCED {experiment_id}':
                b_result = manager.run_experiment_b(
                    experiment_id=experiment_id,
                    a_reproduction_confirmed=True,
                )
                _print_b_result(b_result)
                _flush_print('EXPERIMENT_COMPLETE: closed after B')
                return 0

            if cmd == f'CLOSE {experiment_id}':
                _close_quietly(manager, experiment_id)
                return 0

            _flush_print('INVALID_COMMAND: closing experiment')
            _close_quietly(manager, experiment_id)
            return 1

    except Exception:
        _flush_print('EXCEPTION: closing experiment')
        traceback.print_exc()
        sys.stdout.flush()
        if manager is not None and experiment_id:
            _close_quietly(manager, experiment_id)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
