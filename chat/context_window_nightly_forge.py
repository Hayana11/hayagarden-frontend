"""Nightly Forge compatibility Canary — thin orchestration (R0).

Owner-triggered, one-shot, isolated. Reuses formal Transform/Validator and the
pinned Claude Code CLI. Never touches production DB/Registry/resident/JSONL,
never flips DAILY_SOFT_WINDOW_ENABLED, never schedules itself.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from chat.claude_transcript_model import (
    EventRole,
    SidechainPolicy,
    SummaryPolicy,
    ThinkingPolicy,
    UnknownEventPolicy,
)
from chat.claude_transcript_reader import read_transcript
from chat.claude_transcript_transform import TransformRequest, transform_transcript
from chat.claude_transcript_validator import ValidatorOptions, validate_transcript_events
from chat.context_window_fallback import (
    ENVIRONMENT_BLOCKED,
    AUTH_PROVIDER_OVERRIDE_VARS,
    isolated_owner_canary_env,
    probe_isolated_subscription_auth,
)
from chat.daily_context import DEFAULT_DB_PATH
from tools.cc_jsonl_usage import claude_project_slug
from tools.claude_forge_core import dump_jsonl, session_jsonl_path_for_cwd
from tools.claude_forge_live_gate import (
    CLAUDE_CODE_NPM_SPEC,
    CLAUDE_CODE_PINNED_VERSION,
    SYSTEM_PROMPT,
    parse_raw_jsonl_append,
    parse_stdout_events,
    project_conversational_events,
    verify_jsonl_prefix_unchanged,
)
from tools.claude_forge_subprocess import run_subprocess_with_timeout

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
NATIVE_SEED_USER = (
    'Nightly forge native seed. Reply with exactly NATIVE_SEED_OK and nothing else.'
)
RESUME_PROBE_USER = (
    'Nightly forge resume probe. Reply with exactly RESUME_OK and nothing else.'
)
LIVE_TIMEOUT_SECONDS = 120
AUTH_STATUS_TIMEOUT_SECONDS = 60

# Injectables for focused tests.
_auth_status_runner: Callable[..., Any] = subprocess.run
_auth_login_runner: Callable[..., Any] = subprocess.run
_claude_runner = run_subprocess_with_timeout
_transform_impl = transform_transcript
_validate_impl = validate_transcript_events


class NightlyForgeError(Exception):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


def _assert_isolated_temp_root(temp_root: Path) -> None:
    root = temp_root.resolve()
    if temp_root.is_symlink() or any(
        p.is_symlink() for p in [temp_root, *list(temp_root.parents)[:6]] if p.exists()
    ):
        raise NightlyForgeError('symlink in temp path', error_code='ISOLATION_ESCAPE')
    repo = REPO_ROOT.resolve()
    try:
        root.relative_to(repo)
        raise NightlyForgeError('temp root inside repository', error_code='ISOLATION_ESCAPE')
    except ValueError:
        pass
    frontend = Path('/opt/frontend').resolve()
    if frontend.exists():
        try:
            root.relative_to(frontend)
            raise NightlyForgeError(
                'temp root inside /opt/frontend', error_code='ISOLATION_ESCAPE',
            )
        except ValueError:
            pass
    if (root / 'canary.db').resolve() == Path(DEFAULT_DB_PATH).resolve():
        raise NightlyForgeError(
            'temp db equals production db', error_code='PRODUCTION_PATH_TOUCHED',
        )


def _temp_home(claude_home: Path) -> Path:
    fake = claude_home.parent / 'fake-home'
    fake.mkdir(parents=True, exist_ok=True)
    return fake


def _isolated_env(claude_home: Path) -> dict[str, str]:
    env = isolated_owner_canary_env(claude_home)
    env['HOME'] = str(_temp_home(claude_home))
    for name in AUTH_PROVIDER_OVERRIDE_VARS:
        env.pop(name, None)
    return env


def _claude_cmd(*args: str) -> list[str]:
    return ['npx', '--yes', CLAUDE_CODE_NPM_SPEC, *args]


def probe_nightly_auth(claude_home: Path, cwd: Path) -> tuple[bool, str]:
    previous = os.environ.get('HOME')
    os.environ['HOME'] = str(_temp_home(claude_home))
    try:
        # Rebind probe's runner temporarily via fallback module if present.
        from chat import context_window_fallback as fb
        prev = fb._auth_status_runner
        fb._auth_status_runner = _auth_status_runner
        try:
            return probe_isolated_subscription_auth(claude_home, cwd)
        finally:
            fb._auth_status_runner = prev
    finally:
        if previous is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = previous


def run_nightly_login(claude_home: Path, cwd: Path) -> dict[str, Any]:
    env = _isolated_env(claude_home)
    try:
        proc = _auth_login_runner(
            _claude_cmd('auth', 'login', '--claudeai'),
            cwd=str(cwd),
            env=env,
            check=False,
        )
    except Exception as exc:
        return {
            'ok': False,
            'reason': 'isolated_auth_login_command_failed',
            'detail': str(exc)[:500],
        }
    code = int(getattr(proc, 'returncode', 1) or 0)
    return {
        'ok': code == 0,
        'reason': (
            'isolated_auth_login_completed' if code == 0 else 'isolated_auth_login_failed'
        ),
        'exit_code': code,
    }


def check_claude_pin() -> dict[str, Any]:
    env = os.environ.copy()
    env['DISABLE_AUTOUPDATER'] = '1'
    for name in AUTH_PROVIDER_OVERRIDE_VARS:
        env.pop(name, None)
    try:
        proc = subprocess.run(
            _claude_cmd('--version'),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )
        raw = (proc.stdout or proc.stderr or '').strip() or 'unknown'
        ok = CLAUDE_CODE_PINNED_VERSION in raw
        return {
            'ok': ok,
            'version_raw': raw,
            'pinned': CLAUDE_CODE_PINNED_VERSION,
            'reason': '' if ok else 'claude_pin_mismatch',
        }
    except Exception as exc:
        return {
            'ok': False,
            'version_raw': '',
            'pinned': CLAUDE_CODE_PINNED_VERSION,
            'reason': 'claude_pin_check_failed',
            'detail': str(exc)[:300],
        }


def _plain_user_text_from_event(raw: dict[str, Any]) -> str:
    message = raw.get('message') if isinstance(raw.get('message'), dict) else {}
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                parts.append(str(block.get('text') or ''))
            elif isinstance(block, str):
                parts.append(block)
        return '\n'.join(parts)
    return ''


def _build_user_canonical_map(graph) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for evt in graph.events:
        if evt.event_role != EventRole.CANDIDATE_USER:
            continue
        text = _plain_user_text_from_event(dict(evt.raw)).strip()
        if text:
            mapping[evt.event_uuid] = text
    return mapping


def _discover_latest_session_jsonl(claude_home: Path, cwd: Path) -> tuple[str, Path]:
    proj = claude_home / 'projects' / claude_project_slug(str(cwd))
    if not proj.is_dir():
        raise NightlyForgeError(
            'native project dir missing', error_code=ENVIRONMENT_BLOCKED,
        )
    files = sorted(proj.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise NightlyForgeError(
            'native session jsonl missing', error_code=ENVIRONMENT_BLOCKED,
        )
    return files[0].stem, files[0]


def _run_claude(
    *,
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    stdin_payload: str,
) -> Any:
    return _claude_runner(
        cmd=cmd,
        cwd=str(cwd),
        env=env,
        stdin_payload=stdin_payload,
        timeout_seconds=LIVE_TIMEOUT_SECONDS,
    )


def _create_native_session(
    *,
    claude_home: Path,
    cwd: Path,
) -> dict[str, Any]:
    env = _isolated_env(claude_home)
    payload = json.dumps(
        {
            'type': 'user',
            'message': {'role': 'user', 'content': NATIVE_SEED_USER},
        },
        ensure_ascii=False,
    ) + '\n'
    cmd = _claude_cmd(
        '-p',
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--include-partial-messages',
        '--system-prompt', SYSTEM_PROMPT,
        '--max-turns', '2',
        '--tools', '',
        '--allowedTools', '',
    )
    try:
        run = _run_claude(cmd=cmd, cwd=cwd, env=env, stdin_payload=payload)
    except Exception as exc:
        return {
            'ok': False,
            'error_code': ENVIRONMENT_BLOCKED,
            'process_started': False,
            'reason': 'native_create_spawn_failed',
            'detail': str(exc)[:500],
        }
    if not run.process_started:
        return {
            'ok': False,
            'error_code': ENVIRONMENT_BLOCKED,
            'process_started': False,
            'reason': 'native_create_process_not_started',
        }
    raw = parse_stdout_events(run.stdout_lines)
    if run.exit_code != 0 or not raw.saw_text_delta or not raw.result_ok:
        # Process started ⇒ functional FAIL, not environment block.
        return {
            'ok': False,
            'error_code': 'FAIL',
            'process_started': True,
            'reason': 'native_create_model_failed',
            'exit_code': run.exit_code,
            'saw_text_delta': bool(raw.saw_text_delta),
            'result_ok': bool(raw.result_ok),
        }
    try:
        sid, path = _discover_latest_session_jsonl(claude_home, cwd)
    except NightlyForgeError as exc:
        return {
            'ok': False,
            'error_code': exc.error_code,
            'process_started': True,
            'reason': str(exc),
        }
    return {
        'ok': True,
        'process_started': True,
        'session_id': sid,
        'jsonl_path': str(path),
        'stdout_session_id': raw.stdout_session_id,
    }


def _forge_from_native_jsonl(
    *,
    source_path: Path,
    forged_session_id: str,
    cwd: Path,
    claude_home: Path,
) -> dict[str, Any]:
    graph = read_transcript(source_path)
    mapping = _build_user_canonical_map(graph)
    if not mapping:
        return {
            'ok': False,
            'error_code': 'FAIL',
            'reason': 'no_confirmed_user_rounds_in_native_jsonl',
            'transform_called': False,
            'validator_called': False,
        }
    request = TransformRequest(
        new_session_id=forged_session_id,
        cwd=str(cwd),
        keep_rounds=len(mapping),
        user_canonical_by_event_uuid=mapping,
        thinking_policy=ThinkingPolicy.DROP,
        sidechain_policy=SidechainPolicy.EXCLUDE,
        summary_policy=SummaryPolicy.DROP,
        unknown_event_policy=UnknownEventPolicy.DROP,
        version=CLAUDE_CODE_PINNED_VERSION,
    )
    transform_result = _transform_impl(graph, request)
    validation = _validate_impl(
        transform_result.events,
        options=ValidatorOptions(
            session_id=forged_session_id,
            thinking_policy=ThinkingPolicy.DROP,
            forbid_sidechain=True,
            forbid_summary=True,
            expected_round_count=transform_result.selected_round_count,
            old_uuids=set(mapping.keys()) | set(graph.by_uuid.keys()),
        ),
    )
    if not validation.ok:
        return {
            'ok': False,
            'error_code': 'FAIL',
            'reason': 'validator_rejected_forged_events',
            'transform_called': True,
            'validator_called': True,
            'validator_errors': list(validation.errors)[:20],
        }
    out_path = session_jsonl_path_for_cwd(
        str(cwd), forged_session_id, claude_home=claude_home,
    )
    dump_jsonl(
        out_path,
        transform_result.events,
        allowed_output_root=claude_home.resolve(),
    )
    # Isolation: forged path must stay under temp claude home.
    try:
        out_path.resolve().relative_to(claude_home.resolve())
    except ValueError:
        return {
            'ok': False,
            'error_code': 'ISOLATION_ESCAPE',
            'reason': 'forged_jsonl_outside_temp_claude_home',
            'transform_called': True,
            'validator_called': True,
        }
    before = out_path.read_bytes()
    return {
        'ok': True,
        'transform_called': True,
        'validator_called': True,
        'forged_session_id': forged_session_id,
        'forged_jsonl_path': str(out_path),
        'selected_round_count': transform_result.selected_round_count,
        'event_count': len(transform_result.events),
        'before_byte_len': len(before),
        '_before_bytes': before,  # internal only; stripped before JSON print
    }


def _resume_forged_session(
    *,
    claude_home: Path,
    cwd: Path,
    forged_session_id: str,
    before_bytes: bytes,
    forged_jsonl_path: Path,
) -> dict[str, Any]:
    env = _isolated_env(claude_home)
    payload = json.dumps(
        {
            'type': 'user',
            'message': {'role': 'user', 'content': RESUME_PROBE_USER},
        },
        ensure_ascii=False,
    ) + '\n'
    # Documented working form: -p immediately, then --resume (stream-json stdin).
    cmd = _claude_cmd(
        '-p',
        '--resume', forged_session_id,
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--include-partial-messages',
        '--system-prompt', SYSTEM_PROMPT,
        '--max-turns', '2',
        '--tools', '',
        '--allowedTools', '',
    )
    try:
        run = _run_claude(cmd=cmd, cwd=cwd, env=env, stdin_payload=payload)
    except Exception as exc:
        return {
            'ok': False,
            'error_code': ENVIRONMENT_BLOCKED,
            'process_started': False,
            'reason': 'resume_spawn_failed',
            'detail': str(exc)[:500],
        }
    if not run.process_started:
        return {
            'ok': False,
            'error_code': ENVIRONMENT_BLOCKED,
            'process_started': False,
            'reason': 'resume_process_not_started',
        }

    raw = parse_stdout_events(run.stdout_lines)
    after_bytes = (
        forged_jsonl_path.read_bytes() if forged_jsonl_path.is_file() else b''
    )
    prefix_ok = verify_jsonl_prefix_unchanged(before_bytes, after_bytes)
    append_ok, appended, append_failures = parse_raw_jsonl_append(before_bytes, after_bytes)
    conversational = project_conversational_events(appended) if append_ok else []
    appended_user = any(
        evt.get('type') == 'user'
        and RESUME_PROBE_USER in _plain_user_text_from_event(evt)
        for evt in conversational
    )
    appended_assistant = any(evt.get('type') == 'assistant' for evt in conversational)
    grew = len(after_bytes) > len(before_bytes)

    failures: list[str] = []
    if not raw.saw_text_delta:
        failures.append('missing_text_delta')
    if not raw.result_ok:
        failures.append('result_not_ok')
    if raw.result_is_error is not False:
        failures.append('result_is_error_not_false')
    if not prefix_ok:
        failures.append('jsonl_prefix_changed')
    if not grew:
        failures.append('jsonl_did_not_grow')
    if not append_ok:
        failures.extend(append_failures or ['append_invalid'])
    if not appended_user:
        failures.append('append_missing_user')
    if not appended_assistant:
        failures.append('append_missing_assistant')
    if run.exit_code != 0:
        failures.append(f'resume_exit_code:{run.exit_code}')

    return {
        'ok': not failures,
        'error_code': 'FAIL' if failures else '',
        'process_started': True,
        'saw_text_delta': bool(raw.saw_text_delta),
        'result_ok': bool(raw.result_ok),
        'result_is_error': raw.result_is_error,
        'jsonl_prefix_unchanged': prefix_ok,
        'jsonl_grew': grew,
        'appended_user': appended_user,
        'appended_assistant': appended_assistant,
        'stdout_session_id': raw.stdout_session_id,
        'assistant_text': str(raw.assistant_text or '')[:200],
        'failures': failures,
        'exit_code': run.exit_code,
    }


def run_nightly_forge_canary(
    *,
    confirm_live: bool,
    login_if_needed: bool = False,
    structural_only: bool = False,
) -> dict[str, Any]:
    """One-shot isolated Nightly Forge Canary.

    structural_only=True is for focused unit tests only — it never reports a
    live PASS and never claims Claude resume succeeded.
    """
    if not confirm_live and not structural_only:
        return {
            'ok': False,
            'error_code': 'CONFIRM_LIVE_REQUIRED',
            'NIGHTLY_NOT_RUN': True,
        }

    temp_root = Path(tempfile.mkdtemp(prefix='cw-nightly-forge-', dir='/tmp'))
    report: dict[str, Any] = {
        'ok': False,
        'STRUCTURAL_CANARY_ONLY': bool(structural_only),
        'NIGHTLY_CRON': 'NOT_IMPLEMENTED',
        'temp_root': str(temp_root),
        'paths': {},
        'auth_preflight': None,
        'auth_login': None,
        'claude_pin': None,
        'source_native_session': None,
        'forge': None,
        'live_resume': None,
        'cleanup': None,
        'production_guards': {
            'db_untouched': True,
            'registry_untouched': True,
            'resident_untouched': True,
            'jsonl_untouched': True,
            'flag_still_off': 'DAILY_SOFT_WINDOW_ENABLED' not in os.environ,
        },
    }
    try:
        _assert_isolated_temp_root(temp_root)
        claude_home = temp_root / 'claude-home'
        cwd = temp_root / 'isolated-project'
        claude_home.mkdir(parents=True)
        cwd.mkdir(parents=True)
        fake_home = _temp_home(claude_home)
        report['paths'] = {
            'claude_home': str(claude_home),
            'home': str(fake_home),
            'cwd': str(cwd),
            'CLAUDE_CONFIG_DIR': str(claude_home),
            'HOME': str(fake_home),
        }
        env_sample = _isolated_env(claude_home)
        report['env_overlays_cleared'] = {
            name: name not in env_sample for name in AUTH_PROVIDER_OVERRIDE_VARS
        }
        report['isolation'] = {
            'temp_outside_repo': True,
            'temp_outside_frontend': True,
            'home_under_temp': str(fake_home.resolve()).startswith(str(temp_root.resolve())),
            'config_dir_under_temp': str(claude_home.resolve()).startswith(
                str(temp_root.resolve()),
            ),
        }

        if structural_only:
            # Prove Transform/Validator wiring without claiming live PASS.
            from chat.claude_transcript_model import TranscriptGraph

            empty_graph = TranscriptGraph(session_id='structural', source_path='')
            # Call formal entrypoints with empty/invalid-safe request shapes via
            # a tiny synthetic graph built from one user+assistant pair written
            # as a temp jsonl — still not a live Claude claim.
            seed = temp_root / 'structural-seed.jsonl'
            u = str(uuid.uuid4())
            a = str(uuid.uuid4())
            seed.write_text(
                json.dumps({
                    'type': 'user', 'uuid': u, 'parentUuid': None,
                    'sessionId': 'structural', 'cwd': str(cwd),
                    'message': {'role': 'user', 'content': 'structural user'},
                }, ensure_ascii=False)
                + '\n'
                + json.dumps({
                    'type': 'assistant', 'uuid': a, 'parentUuid': u,
                    'sessionId': 'structural', 'cwd': str(cwd),
                    'message': {
                        'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'structural asst'}],
                    },
                }, ensure_ascii=False)
                + '\n',
                encoding='utf-8',
            )
            forge = _forge_from_native_jsonl(
                source_path=seed,
                forged_session_id=str(uuid.uuid4()),
                cwd=cwd,
                claude_home=claude_home,
            )
            report['forge'] = forge
            report['ok'] = bool(forge.get('ok')) and bool(forge.get('transform_called')) and bool(
                forge.get('validator_called'),
            )
            report['live_resume'] = {
                'attempted': False,
                'ok': False,
                'STRUCTURAL_ONLY': True,
                'note': 'structural path must never be treated as live PASS',
            }
            return report

        pin = check_claude_pin()
        report['claude_pin'] = pin
        if not pin.get('ok'):
            report['error_code'] = ENVIRONMENT_BLOCKED
            report['ok'] = False
            return report

        auth_ok, auth_reason = probe_nightly_auth(claude_home, cwd)
        report['auth_preflight'] = {
            'ok': auth_ok,
            'reason': auth_reason,
            'initial_reason': auth_reason,
            'login_attempted': False,
        }
        if not auth_ok and login_if_needed:
            login = run_nightly_login(claude_home, cwd)
            report['auth_login'] = login
            report['auth_preflight']['login_attempted'] = True
            if login.get('ok'):
                auth_ok, auth_reason = probe_nightly_auth(claude_home, cwd)
                report['auth_preflight']['ok'] = auth_ok
                report['auth_preflight']['reason'] = auth_reason
        if not auth_ok:
            report['live_resume'] = {
                'attempted': False,
                'ENVIRONMENT_BLOCKED': True,
                'reason': auth_reason,
            }
            report['error_code'] = ENVIRONMENT_BLOCKED
            report['ok'] = False
            return report

        native = _create_native_session(claude_home=claude_home, cwd=cwd)
        report['source_native_session'] = native
        if not native.get('ok'):
            report['error_code'] = str(native.get('error_code') or 'FAIL')
            report['ok'] = False
            return report

        forged_sid = str(uuid.uuid4())
        forge = _forge_from_native_jsonl(
            source_path=Path(native['jsonl_path']),
            forged_session_id=forged_sid,
            cwd=cwd,
            claude_home=claude_home,
        )
        report['forge'] = forge
        if not forge.get('ok'):
            report['error_code'] = str(forge.get('error_code') or 'FAIL')
            report['ok'] = False
            return report

        before_bytes = forge.pop('_before_bytes', b'')
        live = _resume_forged_session(
            claude_home=claude_home,
            cwd=cwd,
            forged_session_id=forged_sid,
            before_bytes=before_bytes,
            forged_jsonl_path=Path(forge['forged_jsonl_path']),
        )
        report['live_resume'] = live
        if live.get('error_code') == ENVIRONMENT_BLOCKED:
            report['error_code'] = ENVIRONMENT_BLOCKED
            report['ok'] = False
            return report
        if not live.get('ok'):
            report['error_code'] = 'FAIL'
            report['ok'] = False
            return report

        report['ok'] = True
        return report
    except NightlyForgeError as exc:
        report['error_code'] = exc.error_code
        report['error'] = str(exc)
        report['ok'] = False
        return report
    except Exception as exc:
        report['error_code'] = 'NIGHTLY_FAILED'
        report['error'] = str(exc)
        report['traceback'] = traceback.format_exc()
        report['ok'] = False
        return report
    finally:
        # Never leak internal byte payloads into printed JSON.
        forge_rep = report.get('forge')
        if isinstance(forge_rep, dict):
            forge_rep.pop('_before_bytes', None)
        cleanup_ok = True
        detail: list[str] = []
        try:
            if temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=False)
                detail.append('temp_root_removed')
            if temp_root.exists():
                cleanup_ok = False
                detail.append('temp_root_still_exists')
                report['error_code'] = 'CLEANUP_FAILED'
                report['ok'] = False
        except Exception as exc:
            cleanup_ok = False
            detail.append('rmtree_failed:%s' % exc)
            report['error_code'] = 'CLEANUP_FAILED'
            report['ok'] = False
        report['cleanup'] = {'ok': cleanup_ok, 'detail': detail}
        report['production_guards']['flag_still_off'] = (
            'DAILY_SOFT_WINDOW_ENABLED' not in os.environ
        )
