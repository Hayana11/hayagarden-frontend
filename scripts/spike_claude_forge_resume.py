#!/usr/bin/env python3
"""P-CONTEXT-WINDOW-SPIKE-0: validate forged Claude transcript --resume."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.claude_forge_core import (  # noqa: E402
    ForgeOptions,
    _message_text,
    collect_event_uuids,
    dump_jsonl,
    forge_transcript,
    load_jsonl,
    new_uuid,
    session_jsonl_path_for_cwd,
    sha256_file,
    verify_work_root,
)
from tools.claude_forge_live_gate import (  # noqa: E402
    CLAUDE_CODE_NPM_SPEC,
    CLAUDE_CODE_PINNED_VERSION,
    SYSTEM_PROMPT,
    build_live_user_prompt,
    decide_verdict,
    evaluate_live_gate,
    generate_history_canary,
    parse_raw_jsonl_append,
    parse_stdout_events,
    prepare_history_for_live_gate,
    project_conversational_events,
)
from tools.claude_forge_subprocess import SubprocessRunResult, run_subprocess_with_timeout  # noqa: E402
from tools.claude_forge_validator import validate_forged_transcript  # noqa: E402
from tools.cc_jsonl_usage import claude_project_slug  # noqa: E402

CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'npx')
CLAUDE_ARGS_PREFIX = (
    ['--yes', CLAUDE_CODE_NPM_SPEC]
    if CLAUDE_BIN == 'npx'
    else []
)
PROBE_TIMEOUT_SECONDS = 120
REDACT_PATTERNS = (
    (re.compile(r'(Bearer\s+)\S+', re.I), r'\1[REDACTED]'),
    (re.compile(r'(api[_-]?key["\']?\s*[:=]\s*)["\']?[\w-]+', re.I), r'\1[REDACTED]'),
    (re.compile(r'(toolu_)[A-Za-z0-9]+'), r'\1[REDACTED]'),
    (re.compile(r'sk-ant-[A-Za-z0-9_-]+'), '[REDACTED]'),
)

# Injectable in integration tests.
_subprocess_runner: Callable[..., SubprocessRunResult] = run_subprocess_with_timeout


def redact(text: str) -> str:
    out = text or ''
    for pattern, repl in REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    if len(out) > 2000:
        out = out[:2000] + '…[truncated]'
    return out


def claude_version_check() -> tuple[str, bool, str]:
    env = os.environ.copy()
    env['DISABLE_AUTOUPDATER'] = '1'
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, *CLAUDE_ARGS_PREFIX, '--version'],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )
        raw = (proc.stdout or proc.stderr or '').strip() or 'unknown'
        ok = CLAUDE_CODE_PINNED_VERSION in raw
        return raw, ok, '' if ok else f'expected {CLAUDE_CODE_PINNED_VERSION}, got {raw}'
    except Exception as exc:
        return f'error:{type(exc).__name__}', False, str(exc)


def explicit_auth_available() -> tuple[bool, str]:
    if os.environ.get('ANTHROPIC_API_KEY', '').strip():
        return True, 'ANTHROPIC_API_KEY'
    if os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '').strip():
        return True, 'CLAUDE_CODE_OAUTH_TOKEN'
    return False, 'missing'


def isolated_claude_env(claude_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env['CLAUDE_CONFIG_DIR'] = str(claude_home)
    env['DISABLE_AUTOUPDATER'] = '1'
    return env


def git_tree_sha() -> str:
    out = _git(['write-tree'])
    return out or _git(['rev-parse', 'HEAD'])


def git_diff_sha256() -> str:
    diff = _git(['diff', 'HEAD'])
    if not diff:
        diff = _git(['diff', '--cached'])
    return hashlib.sha256(diff.encode('utf-8')).hexdigest()


@dataclass
class CaseResult:
    case_id: str
    name: str
    source_sha256: str = ''
    target_sha256: str = ''
    event_count: int = 0
    first_type: str = ''
    last_type: str = ''
    structure_ok: bool = False
    process_started: bool = False
    first_delta: bool = False
    result_ok: bool = False
    transcript_grew: bool = False
    exit_code: Optional[int] = None
    error_type: str = ''
    error_detail: str = ''
    state: str = 'NOT_RUN'
    notes: str = ''
    canary: str = ''
    canary_matched: bool = False
    live_gate_failures: list[str] = field(default_factory=list)
    live_gate_warnings: list[str] = field(default_factory=list)
    native_create_sha256: str = ''
    native_before_sha256: str = ''
    native_jsonl_rewritten: Optional[bool] = None
    native_stdout_session_id: str = ''
    native_jsonl_session_id: str = ''


@dataclass
class SpikeReport:
    branch: str = ''
    tested_commit_sha: str = ''
    tested_tree_sha: str = ''
    tested_diff_sha256: str = ''
    artifact_commit_sha: str = 'artifact_commit_pending'
    unit_test_command: str = ''
    unit_test_exit_code: Optional[int] = None
    unit_test_count: int = 0
    harness_command: str = ''
    harness_exit_code: Optional[int] = None
    generated_at: str = ''
    command: str = ''
    command_exit_code: Optional[int] = None
    origin_main_sha: str = ''
    claude_version: str = ''
    claude_version_ok: bool = False
    auth_available: bool = False
    auth_source: str = ''
    live_probe_status: str = 'NOT_RUN'
    ci_verified: bool = False
    isolated_cwd: str = ''
    claude_home: str = ''
    touched_production: bool = False
    structural_only: bool = False
    cases: list[CaseResult] = field(default_factory=list)
    verdict: str = 'NO-GO'
    verdict_reason: str = ''

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in self.__dict__.items() if k != 'cases'},
            'cases': [asdict(c) for c in self.cases],
        }


def _git(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(
            ['git', *cmd],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=False,
        )
        return (proc.stdout or proc.stderr).strip()
    except Exception:
        return ''


def _claude_cmd(*args: str) -> list[str]:
    return [CLAUDE_BIN, *CLAUDE_ARGS_PREFIX, *args]


def _run_claude(
    *,
    cmd: list[str],
    cwd: str,
    env: dict[str, str],
    stdin_payload: str,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    popen_factory: Optional[Callable[..., Any]] = None,
) -> SubprocessRunResult:
    return _subprocess_runner(
        cmd=cmd,
        cwd=cwd,
        env=env,
        stdin_payload=stdin_payload,
        timeout_seconds=timeout_seconds,
        popen_factory=popen_factory,
    )


def _resume_probe_from_run(
    *,
    run: SubprocessRunResult,
    session_id: str,
    canary: str,
    before_bytes: bytes,
    before_events: list[dict[str, Any]],
    jsonl_path: Path,
    old_uuids: Optional[set[str]] = None,
) -> dict[str, Any]:
    raw = parse_stdout_events(run.stdout_lines)
    raw.exit_code = run.exit_code
    raw.stderr_text = run.stderr_text
    raw.timed_out = run.timed_out
    raw.process_started = run.process_started
    if run.timed_out:
        raw.error_type = 'timeout'
        raw.error_detail = 'resume probe timed out'
    elif run.stderr_text and not raw.error_detail:
        raw.error_detail = redact(run.stderr_text)

    after_bytes = jsonl_path.read_bytes() if jsonl_path.is_file() else b''
    _, appended_events, _ = parse_raw_jsonl_append(before_bytes, after_bytes)
    after_events = list(before_events) + appended_events

    gate = evaluate_live_gate(
        raw=raw,
        expected_session_id=session_id,
        canary=canary,
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        before_events=before_events,
        after_events=after_events,
        old_uuids=old_uuids,
    )
    return {
        'process_started': run.process_started,
        'first_delta': raw.saw_text_delta,
        'result_ok': raw.result_ok,
        'transcript_grew': len(after_bytes) > len(before_bytes),
        'exit_code': run.exit_code,
        'error_type': raw.error_type,
        'error_detail': redact(raw.error_detail),
        'state': gate.state,
        'canary_matched': gate.canary_matched,
        'live_gate_failures': gate.failures,
        'live_gate_warnings': gate.warnings,
    }


def _spawn_resume_probe(
    *,
    cwd: str,
    session_id: str,
    claude_home: Path,
    canary: str,
    before_bytes: bytes,
    before_events: list[dict[str, Any]],
    old_uuids: Optional[set[str]] = None,
    allowed_tools: str = '',
    popen_factory: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    env = isolated_claude_env(claude_home)
    cmd = _claude_cmd(
        '-p',
        '--resume', session_id,
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--include-partial-messages',
        '--system-prompt', SYSTEM_PROMPT,
        '--max-turns', '3',
        '--tools', '',
        '--allowedTools', allowed_tools,
    )
    payload = json.dumps(
        {'type': 'user', 'message': {'role': 'user', 'content': build_live_user_prompt()}},
        ensure_ascii=False,
    ) + '\n'
    path = session_jsonl_path_for_cwd(cwd, session_id, claude_home=claude_home)
    run = _run_claude(
        cmd=cmd,
        cwd=cwd,
        env=env,
        stdin_payload=payload,
        popen_factory=popen_factory,
    )
    return _resume_probe_from_run(
        run=run,
        session_id=session_id,
        canary=canary,
        before_bytes=before_bytes,
        before_events=before_events,
        jsonl_path=path,
        old_uuids=old_uuids,
    )


def _discover_latest_session_jsonl(claude_home: Path, cwd: str) -> tuple[str, Path]:
    proj = claude_home / 'projects' / claude_project_slug(cwd)
    files = sorted(proj.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError('no native session jsonl created')
    path = files[0]
    return path.stem, path


def _run_case0_native_control(
    *,
    isolated_cwd: Path,
    claude_home: Path,
    work_root: Path,
    popen_factory: Optional[Callable[..., Any]] = None,
) -> CaseResult:
    result = CaseResult(case_id='0', name='control_native_session_resume')
    env = isolated_claude_env(claude_home)
    canary = generate_history_canary()
    result.canary = canary
    first_payload = json.dumps(
        {
            'type': 'user',
            'message': {
                'role': 'user',
                'content': f'请只回复以下校验码，不要添加任何其他字符：{canary}',
            },
        },
        ensure_ascii=False,
    ) + '\n'
    create_cmd = _claude_cmd(
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
    create_run = _run_claude(
        cmd=create_cmd,
        cwd=str(isolated_cwd),
        env=env,
        stdin_payload=first_payload,
        popen_factory=popen_factory,
    )
    create_raw = parse_stdout_events(create_run.stdout_lines)
    create_raw.process_started = create_run.process_started
    create_raw.exit_code = create_run.exit_code
    result.process_started = create_run.process_started
    result.exit_code = create_run.exit_code
    result.first_delta = create_raw.saw_text_delta
    result.result_ok = create_raw.result_ok
    result.canary_matched = create_raw.assistant_text.strip() == canary
    result.native_stdout_session_id = create_raw.stdout_session_id
    failures: list[str] = []
    if not create_run.process_started:
        failures.append('native_create_process_not_started')
    if create_run.exit_code != 0:
        failures.append(f'native_create_exit_code:{create_run.exit_code}')
    if not create_raw.saw_text_delta:
        failures.append('native_create_missing_text_delta')
    if not create_raw.result_ok:
        failures.append('native_create_result_not_ok')
    if create_raw.result_is_error is not False:
        failures.append('native_create_result_is_error_not_false')
    if not create_raw.stdout_session_id:
        failures.append('native_create_missing_stdout_session_id')
    if create_raw.assistant_text.strip() != canary:
        failures.append('native_create_stdout_canary_mismatch')

    try:
        _, jsonl_path = _discover_latest_session_jsonl(claude_home, str(isolated_cwd))
    except Exception as exc:
        result.state = 'NATIVE_CREATE_FAIL'
        result.error_type = 'native_create_jsonl_missing'
        result.error_detail = redact(str(exc))
        result.live_gate_failures = failures + ['native_create_jsonl_missing']
        return result

    session_id = create_raw.stdout_session_id
    result.native_jsonl_session_id = jsonl_path.stem
    if jsonl_path.stem != session_id:
        failures.append('native_create_filename_session_mismatch')

    try:
        base_events = load_jsonl(jsonl_path)
        create_bytes = jsonl_path.read_bytes()
    except Exception as exc:
        failures.append(f'native_create_jsonl_invalid:{type(exc).__name__}')
        result.state = 'NATIVE_CREATE_FAIL'
        result.error_type = failures[0]
        result.error_detail = redact(str(exc))
        result.live_gate_failures = list(dict.fromkeys(failures))
        return result

    conversation = project_conversational_events(base_events)
    if any(str(evt.get('sessionId') or '') != session_id for evt in conversation):
        failures.append('native_create_jsonl_session_mismatch')
    assistant_events = [evt for evt in conversation if evt.get('type') == 'assistant']
    disk_text = (
        _message_text((assistant_events[-1].get('message') or {}).get('content')).strip()
        if assistant_events else ''
    )
    if disk_text != canary:
        failures.append('native_create_jsonl_canary_mismatch')

    create_sha = hashlib.sha256(create_bytes).hexdigest()
    result.source_sha256 = create_sha
    result.native_create_sha256 = create_sha
    result.event_count = len(base_events)
    result.structure_ok = not failures
    before_bytes = jsonl_path.read_bytes()
    result.native_before_sha256 = hashlib.sha256(before_bytes).hexdigest()
    result.native_jsonl_rewritten = before_bytes != create_bytes
    if result.native_jsonl_rewritten:
        failures.append('native_create_jsonl_rewritten')
    if failures:
        result.state = 'NATIVE_CREATE_FAIL'
        result.error_type = failures[0]
        result.error_detail = redact(create_run.stderr_text or ';'.join(failures))
        result.live_gate_failures = list(dict.fromkeys(failures))
        return result

    before_events = base_events
    probe = _spawn_resume_probe(
        cwd=str(isolated_cwd),
        session_id=session_id,
        claude_home=claude_home,
        canary=canary,
        before_bytes=before_bytes,
        before_events=before_events,
        popen_factory=popen_factory,
    )
    _apply_probe_to_case(result, probe)
    return result


def _apply_probe_to_case(result: CaseResult, probe: dict[str, Any]) -> None:
    result.process_started = probe['process_started']
    result.first_delta = probe['first_delta']
    result.result_ok = probe['result_ok']
    result.transcript_grew = probe['transcript_grew']
    result.exit_code = probe.get('exit_code')
    result.error_type = probe.get('error_type') or ''
    result.error_detail = probe.get('error_detail') or ''
    result.canary_matched = probe.get('canary_matched', False)
    result.live_gate_failures = list(probe.get('live_gate_failures') or [])
    result.live_gate_warnings = list(probe.get('live_gate_warnings') or [])
    result.state = probe.get('state') or 'RESUME_FAIL'


def _record_forge_case(
    report: SpikeReport,
    *,
    case_id: str,
    name: str,
    source_path: Path,
    forged_events: list[dict],
    new_session_id: str,
    work_root: Path,
    isolated_cwd: Path,
    claude_home: Path,
    old_uuids: set[str],
    run_live: bool,
    popen_factory: Optional[Callable[..., Any]] = None,
) -> CaseResult:
    result = CaseResult(case_id=case_id, name=name)
    result.source_sha256 = sha256_file(source_path)
    result.event_count = len(forged_events)
    result.first_type = str(forged_events[0].get('type') if forged_events else '')
    result.last_type = str(forged_events[-1].get('type') if forged_events else '')

    canary = generate_history_canary() if run_live else ''
    if run_live:
        result.canary = canary
    events_to_write = (
        prepare_history_for_live_gate(
            forged_events,
            canary,
            session_id=new_session_id,
            cwd=str(isolated_cwd),
        )
        if run_live
        else forged_events
    )

    out_path = session_jsonl_path_for_cwd(str(isolated_cwd), new_session_id, claude_home=claude_home)
    result.target_sha256 = dump_jsonl(
        out_path,
        events_to_write,
        allowed_output_root=work_root,
    )

    validation = validate_forged_transcript(
        events_to_write,
        session_id=new_session_id,
        output_path=out_path,
        expected_sha256=result.target_sha256,
        allowed_output_root=work_root,
        old_uuids=old_uuids,
    )
    result.structure_ok = validation.ok
    if not validation.ok:
        result.error_type = 'structure'
        result.error_detail = redact(';'.join(validation.errors[:5]))
        result.state = 'STRUCTURE_FAIL'
        report.cases.append(result)
        return result

    if not run_live:
        result.state = 'STRUCTURE_ONLY'
        result.notes = report.live_probe_status
        report.cases.append(result)
        return result

    before_bytes = out_path.read_bytes()
    before_events = load_jsonl(out_path)
    probe = _spawn_resume_probe(
        cwd=str(isolated_cwd),
        session_id=new_session_id,
        claude_home=claude_home,
        canary=canary,
        before_bytes=before_bytes,
        before_events=before_events,
        old_uuids=old_uuids,
        popen_factory=popen_factory,
    )
    _apply_probe_to_case(result, probe)
    report.cases.append(result)
    return result


def run_spike(
    work_root: Optional[Path] = None,
    *,
    structural_only: bool = False,
    popen_factory: Optional[Callable[..., Any]] = None,
    command: str = '',
    unit_test_command: str = '',
    unit_test_exit_code: Optional[int] = None,
    unit_test_count: int = 0,
    artifact_commit_sha: str = 'artifact_commit_pending',
) -> SpikeReport:
    report = SpikeReport()
    report.structural_only = structural_only
    report.branch = _git(['branch', '--show-current'])
    report.tested_commit_sha = _git(['rev-parse', 'HEAD'])
    report.tested_tree_sha = git_tree_sha()
    report.tested_diff_sha256 = git_diff_sha256()
    report.artifact_commit_sha = artifact_commit_sha
    report.unit_test_command = unit_test_command
    report.unit_test_exit_code = unit_test_exit_code
    report.unit_test_count = unit_test_count
    report.harness_command = command
    report.generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report.command = command
    report.origin_main_sha = _git(['rev-parse', 'origin/main'])
    if structural_only:
        version_raw = f'{CLAUDE_CODE_PINNED_VERSION} (pinned; structural-only did not execute Claude)'
        version_ok = True
        version_err = ''
    else:
        version_raw, version_ok, version_err = claude_version_check()
    report.claude_version = version_raw
    report.claude_version_ok = version_ok
    report.touched_production = False
    report.ci_verified = False

    if structural_only:
        report.auth_available = False
        report.auth_source = 'ignored_structural_only'
        report.live_probe_status = 'NOT_RUN_NO_CREDENTIALS'
        run_live = False
    else:
        ok, src = explicit_auth_available()
        report.auth_available = ok
        report.auth_source = src
        if not ok:
            report.live_probe_status = 'NOT_RUN_NO_CREDENTIALS'
            run_live = False
        elif not version_ok:
            report.live_probe_status = 'NOT_RUN_VERSION_MISMATCH'
            run_live = False
        else:
            report.live_probe_status = 'RUN'
            run_live = True

    raw_work_root = work_root or Path(tempfile.mkdtemp(prefix='claude-forge-spike-'))
    work_root = verify_work_root(raw_work_root)
    isolated_cwd = work_root / 'isolated-project'
    isolated_cwd.mkdir(parents=True, exist_ok=True)
    claude_home = work_root / 'claude-home'
    claude_home.mkdir(parents=True, exist_ok=True)
    report.isolated_cwd = str(isolated_cwd)
    report.claude_home = str(claude_home)

    fixture_root = ROOT / 'tests' / 'fixtures' / 'claude_forge_spike'
    if not fixture_root.is_dir():
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_claude_forge_fixtures.py')], check=True)

    if run_live:
        c0 = _run_case0_native_control(
            isolated_cwd=isolated_cwd,
            claude_home=claude_home,
            work_root=work_root,
            popen_factory=popen_factory,
        )
        report.cases.append(c0)
    else:
        c0 = CaseResult(case_id='0', name='control_native_session_resume')
        c0.state = 'NOT_RUN_NO_CREDENTIALS'
        c0.notes = report.live_probe_status
        report.cases.append(c0)

    def _forge_case(case_id: str, name: str, src: Path, opts: ForgeOptions, old_uuids: set[str]) -> None:
        forged = forge_transcript(load_jsonl(src), opts)
        _record_forge_case(
            report,
            case_id=case_id,
            name=name,
            source_path=src,
            forged_events=forged.events,
            new_session_id=opts.new_session_id,
            work_root=work_root,
            isolated_cwd=isolated_cwd,
            claude_home=claude_home,
            old_uuids=old_uuids,
            run_live=run_live,
            popen_factory=popen_factory,
        )

    src1 = fixture_root / 'case1_plain_two_rounds.jsonl'
    _forge_case('1', 'minimal_plain_text_forge', src1, ForgeOptions(
        new_session_id=new_uuid(), cwd=str(isolated_cwd), allowed_output_root=work_root,
    ), collect_event_uuids(load_jsonl(src1)))

    src2 = fixture_root / 'case2a_signed_thinking.jsonl'
    old2 = collect_event_uuids(load_jsonl(src2))
    _forge_case('2A', 'signed_thinking_preserve', src2, ForgeOptions(
        new_session_id=new_uuid(), cwd=str(isolated_cwd), keep_thinking=True, allowed_output_root=work_root,
    ), old2)
    _forge_case('2B', 'signed_thinking_drop_block', src2, ForgeOptions(
        new_session_id=new_uuid(), cwd=str(isolated_cwd), keep_thinking=False, drop_thinking=True,
        allowed_output_root=work_root,
    ), old2)

    src3 = fixture_root / 'case3_tool_success.jsonl'
    _forge_case('3A', 'tool_round_success', src3, ForgeOptions(
        new_session_id=new_uuid(), cwd=str(isolated_cwd), allowed_output_root=work_root,
    ), collect_event_uuids(load_jsonl(src3)))

    src5 = fixture_root / 'case5_sidechain.jsonl'
    _forge_case('5B', 'sidechain_exclude', src5, ForgeOptions(
        new_session_id=new_uuid(), cwd=str(isolated_cwd), exclude_sidechain=True, allowed_output_root=work_root,
    ), collect_event_uuids(load_jsonl(src5)))

    src6 = fixture_root / 'case6_summary_uuid_refs.jsonl'
    _forge_case('6', 'summary_drop_uuid_scan', src6, ForgeOptions(
        new_session_id=new_uuid(), cwd=str(isolated_cwd), allowed_output_root=work_root,
    ), collect_event_uuids(load_jsonl(src6)))

    src7 = fixture_root / 'case7_legacy_injection.jsonl'
    canon = json.loads((fixture_root / 'app_db_canonical_messages.json').read_text(encoding='utf-8'))
    ev7 = load_jsonl(src7)
    inj_uuid = next(str(e.get('uuid')) for e in ev7 if e.get('type') == 'user')
    _forge_case('7', 'legacy_injection_strip', src7, ForgeOptions(
        new_session_id=new_uuid(),
        cwd=str(isolated_cwd),
        allowed_output_root=work_root,
        user_canonical_by_event_uuid={inj_uuid: canon['by_claude_event_uuid'][inj_uuid]},
    ), collect_event_uuids(ev7))

    for label, events in (
        ('empty_thinking', [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': new_uuid(),
             'message': {'role': 'user', 'content': 'x'}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': new_uuid(),
             'message': {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': ''}]}},
        ]),
        ('orphan_tool_use', [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': new_uuid(),
             'message': {'role': 'user', 'content': 'x'}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': new_uuid(),
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': 'toolu_orphan', 'name': 'Read', 'input': {}},
             ]}},
        ]),
    ):
        c8 = CaseResult(case_id='8', name=label)
        sid8 = new_uuid()
        u = events[0]['uuid']
        for e in events:
            e['sessionId'] = sid8
        events[1]['parentUuid'] = u
        v = validate_forged_transcript(events, session_id=sid8)
        c8.structure_ok = v.ok
        c8.state = 'STRUCTURE_REJECT' if not v.ok else 'UNEXPECTED_PASS'
        c8.error_detail = redact(';'.join(v.errors[:3]))
        report.cases.append(c8)

    verdict, reason = decide_verdict(
        [asdict(c) for c in report.cases],
        structural_only=structural_only,
        live_probe_status=report.live_probe_status,
    )
    report.verdict = verdict
    report.verdict_reason = reason
    if not version_ok:
        report.verdict = 'NO-GO'
        version_note = f'Claude Code 版本锁定失败: {version_err}'
        report.verdict_reason = f'{version_note}; {reason}' if reason else version_note
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description='Claude forge resume spike harness')
    parser.add_argument('--work-root', type=Path, default=None)
    parser.add_argument('--report', type=Path, default=ROOT / 'artifacts' / 'spike-claude-forge-resume' / 'results.json')
    parser.add_argument('--structural-only', action='store_true', help='Skip live resume probes')
    parser.add_argument('--unit-test-command', default='')
    parser.add_argument('--unit-test-exit-code', type=int, default=None)
    parser.add_argument('--unit-test-count', type=int, default=0)
    parser.add_argument('--artifact-commit-sha', default='artifact_commit_pending')
    args = parser.parse_args()
    cmd_str = ' '.join([sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])
    report = run_spike(
        args.work_root,
        structural_only=args.structural_only,
        command=cmd_str,
        unit_test_command=args.unit_test_command,
        unit_test_exit_code=args.unit_test_exit_code,
        unit_test_count=args.unit_test_count,
        artifact_commit_sha=args.artifact_commit_sha,
    )
    exit_code = 0 if report.verdict == 'GO' else 2
    report.command_exit_code = exit_code
    report.harness_exit_code = exit_code
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report.to_dict(), ensure_ascii=True, indent=2))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
