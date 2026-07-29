#!/usr/bin/env python3
"""P-CONTEXT-WINDOW-SPIKE-0: validate forged Claude transcript --resume.

Isolation only — never touches production VPS, real transcripts, or secrets in logs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.claude_forge_core import (  # noqa: E402
    ForgeOptions,
    build_minimal_text_session,
    dump_jsonl,
    forge_transcript,
    load_jsonl,
    new_uuid,
    session_jsonl_path_for_cwd,
    sha256_file,
)
from tools.claude_forge_validator import validate_forged_transcript  # noqa: E402

CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'npx')
CLAUDE_ARGS_PREFIX = (
    ['--yes', '@anthropic-ai/claude-code']
    if CLAUDE_BIN == 'npx'
    else []
)
SYSTEM_PROMPT = '你是隔离 Spike 测试助手。只回复简短确认，不要调用工具。'
PROBE_MESSAGE = '请只回复两个字：收到'
REDACT_PATTERNS = (
    (re.compile(r'(Bearer\s+)\S+', re.I), r'\1[REDACTED]'),
    (re.compile(r'(api[_-]?key["\']?\s*[:=]\s*)["\']?[\w-]+', re.I), r'\1[REDACTED]'),
    (re.compile(r'(toolu_)[A-Za-z0-9]+'), r'\1[REDACTED]'),
    (re.compile(r'sk-ant-[A-Za-z0-9_-]+'), '[REDACTED]'),
)


def redact(text: str) -> str:
    out = text or ''
    for pattern, repl in REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    if len(out) > 2000:
        out = out[:2000] + '…[truncated]'
    return out


def claude_version() -> str:
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, *CLAUDE_ARGS_PREFIX, '--version'],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return (proc.stdout or proc.stderr or '').strip() or 'unknown'
    except Exception as exc:
        return f'error:{type(exc).__name__}'


def auth_available() -> tuple[bool, str]:
    if os.environ.get('ANTHROPIC_API_KEY', '').strip():
        return True, 'ANTHROPIC_API_KEY'
    if os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '').strip():
        return True, 'CLAUDE_CODE_OAUTH_TOKEN'
    # oauth session files under ~/.claude/sessions
    sessions = Path.home() / '.claude' / 'sessions'
    if sessions.is_dir() and any(sessions.iterdir()):
        return True, 'claude_local_session'
    return False, 'missing'


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


@dataclass
class SpikeReport:
    branch: str = ''
    head_sha: str = ''
    origin_main_sha: str = ''
    claude_version: str = ''
    auth_available: bool = False
    auth_source: str = ''
    isolated_cwd: str = ''
    claude_home: str = ''
    touched_production: bool = False
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
            check=False,
        )
        return (proc.stdout or proc.stderr).strip()
    except Exception:
        return ''


def _spawn_resume_probe(
    *,
    cwd: str,
    session_id: str,
    claude_home: Path,
    allowed_tools: str = '',
) -> dict[str, Any]:
    env = os.environ.copy()
    env['CLAUDE_CONFIG_DIR'] = str(claude_home)
    cmd = [
        CLAUDE_BIN,
        *CLAUDE_ARGS_PREFIX,
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
    ]
    payload = json.dumps(
        {'type': 'user', 'message': {'role': 'user', 'content': PROBE_MESSAGE}},
        ensure_ascii=False,
    )
    path_before = session_jsonl_path_for_cwd(cwd, session_id, claude_home=claude_home)
    size_before = path_before.stat().st_size if path_before.is_file() else 0

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out: dict[str, Any] = {
        'process_started': True,
        'first_delta': False,
        'result_ok': False,
        'exit_code': None,
        'error_type': '',
        'error_detail': '',
        'transcript_grew': False,
    }
    try:
        assert proc.stdin is not None
        proc.stdin.write(payload + '\n')
        proc.stdin.flush()
        proc.stdin.close()
        assert proc.stdout is not None
        deadline = time.time() + 120
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line == '':
                break
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get('type')
            if etype in {'assistant', 'content_block_delta', 'stream_event'}:
                out['first_delta'] = True
            if etype == 'result':
                out['result_ok'] = not bool(evt.get('is_error'))
            message = evt.get('message') or {}
            content = message.get('content')
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        out['first_delta'] = True
            if isinstance(evt.get('delta'), dict):
                out['first_delta'] = True
        proc.wait(timeout=30)
        out['exit_code'] = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out['error_type'] = 'timeout'
        out['error_detail'] = 'resume probe timed out'
    except Exception as exc:
        out['error_type'] = type(exc).__name__
        out['error_detail'] = redact(str(exc))
    finally:
        if proc.stderr:
            err = proc.stderr.read() or ''
            if err and not out['error_detail']:
                out['error_detail'] = redact(err)
        size_after = path_before.stat().st_size if path_before.is_file() else 0
        out['transcript_grew'] = size_after > size_before
    if out['exit_code'] not in (0, None) and not out['error_type']:
        out['error_type'] = f'exit_{out["exit_code"]}'
    return out


def _create_baseline_session(cwd: Path, claude_home: Path) -> tuple[str, Path]:
    """CASE 0: let Claude Code create a real 2-round session when auth exists."""
    session_id = new_uuid()
    env = os.environ.copy()
    env['CLAUDE_CONFIG_DIR'] = str(claude_home)
    cmd = [
        CLAUDE_BIN,
        *CLAUDE_ARGS_PREFIX,
        '-p', '回复：测试回复 A',
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--include-partial-messages',
        '--system-prompt', SYSTEM_PROMPT,
        '--max-turns', '2',
        '--tools', '',
        '--allowedTools', '',
    ]
    first = json.dumps(
        {'type': 'user', 'message': {'role': 'user', 'content': '测试消息 A'}},
        ensure_ascii=False,
    )
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        input=first + '\n',
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(redact(proc.stderr or proc.stdout or 'baseline create failed'))
    # discover session id from ~/.claude/projects/<slug>/
    slug = cwd.name if False else None
    from tools.cc_jsonl_usage import claude_project_slug

    proj = claude_home / 'projects' / claude_project_slug(str(cwd))
    files = sorted(proj.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError('no jsonl created in isolated project dir')
    path = files[0]
    session_id = path.stem
    return session_id, path


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
    old_uuids: Optional[set[str]] = None,
    run_resume: bool = True,
) -> CaseResult:
    result = CaseResult(case_id=case_id, name=name)
    source_events = load_jsonl(source_path)
    result.source_sha256 = sha256_file(source_path)
    result.event_count = len(forged_events)
    result.first_type = str(forged_events[0].get('type') if forged_events else '')
    result.last_type = str(forged_events[-1].get('type') if forged_events else '')

    out_path = session_jsonl_path_for_cwd(str(isolated_cwd), new_session_id, claude_home=claude_home)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.target_sha256 = dump_jsonl(out_path, forged_events)

    validation = validate_forged_transcript(
        forged_events,
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

    if not report.auth_available or not run_resume:
        result.state = 'STRUCTURE_ONLY' if not report.auth_available else 'STRUCTURE_OK'
        result.notes = 'live resume skipped (no auth)' if not report.auth_available else ''
        report.cases.append(result)
        return result

    probe = _spawn_resume_probe(
        cwd=str(isolated_cwd),
        session_id=new_session_id,
        claude_home=claude_home,
    )
    result.process_started = probe['process_started']
    result.first_delta = probe['first_delta']
    result.result_ok = probe['result_ok']
    result.transcript_grew = probe['transcript_grew']
    result.exit_code = probe.get('exit_code')
    result.error_type = probe.get('error_type') or ''
    result.error_detail = probe.get('error_detail') or ''

    if result.first_delta and result.result_ok and result.transcript_grew:
        result.state = 'API_ACCEPTED_FIRST_DELTA'
    elif result.process_started and not result.first_delta:
        result.state = 'PROCESS_STARTED'
    else:
        result.state = 'RESUME_FAIL'

    report.cases.append(result)
    return result


def run_spike(work_root: Optional[Path] = None, *, live: bool = True) -> SpikeReport:
    report = SpikeReport()
    report.branch = _git(['branch', '--show-current'])
    report.head_sha = _git(['rev-parse', 'HEAD'])
    report.origin_main_sha = _git(['rev-parse', 'origin/main'])
    report.claude_version = claude_version()
    ok, src = auth_available()
    report.auth_available = ok
    report.auth_source = src
    report.touched_production = False

    work_root = work_root or Path(tempfile.mkdtemp(prefix='claude-forge-spike-'))
    work_root = work_root.resolve()
    isolated_cwd = work_root / 'isolated-project'
    isolated_cwd.mkdir(parents=True, exist_ok=True)
    claude_home = work_root / 'claude-home'
    claude_home.mkdir(parents=True, exist_ok=True)
    report.isolated_cwd = str(isolated_cwd)
    report.claude_home = str(claude_home)

    fixture_root = ROOT / 'tests' / 'fixtures' / 'claude_forge_spike'
    if not fixture_root.is_dir():
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_claude_forge_fixtures.py')], check=True)

    # CASE 0 control
    c0 = CaseResult(case_id='0', name='control_native_session_resume')
    report.cases.append(c0)
    if live and report.auth_available:
        try:
            sid0, path0 = _create_baseline_session(isolated_cwd, claude_home)
            c0.source_sha256 = sha256_file(path0)
            c0.event_count = sum(1 for _ in path0.open() if _.strip())
            probe = _spawn_resume_probe(cwd=str(isolated_cwd), session_id=sid0, claude_home=claude_home)
            c0.process_started = probe['process_started']
            c0.first_delta = probe['first_delta']
            c0.result_ok = probe['result_ok']
            c0.transcript_grew = probe['transcript_grew']
            c0.exit_code = probe.get('exit_code')
            c0.error_type = probe.get('error_type') or ''
            c0.error_detail = probe.get('error_detail') or ''
            c0.structure_ok = True
            c0.state = 'API_ACCEPTED_FIRST_DELTA' if c0.first_delta and c0.result_ok else 'RESUME_FAIL'
        except Exception as exc:
            c0.error_type = type(exc).__name__
            c0.error_detail = redact(str(exc))
            c0.state = 'ENV_FAIL'
    else:
        c0.state = 'BLOCKED_MISSING_CREDENTIALS'
        c0.notes = 'ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN not available in this environment'

    # CASE 1 minimal plain forge
    src1 = fixture_root / 'case1_plain_two_rounds.jsonl'
    old_events = load_jsonl(src1)
    old_uuids = {str(e.get('uuid')) for e in old_events if e.get('uuid')}
    new_sid = new_uuid()
    forged1 = forge_transcript(
        old_events,
        ForgeOptions(new_session_id=new_sid, cwd=str(isolated_cwd), allowed_output_root=work_root),
    )
    _record_forge_case(
        report,
        case_id='1',
        name='minimal_plain_text_forge',
        source_path=src1,
        forged_events=forged1.events,
        new_session_id=new_sid,
        work_root=work_root,
        isolated_cwd=isolated_cwd,
        claude_home=claude_home,
        old_uuids=old_uuids,
        run_resume=live,
    )

    # CASE 2A signed thinking keep
    src2 = fixture_root / 'case2a_signed_thinking.jsonl'
    ev2 = load_jsonl(src2)
    sid2a = new_uuid()
    f2a = forge_transcript(ev2, ForgeOptions(new_session_id=sid2a, cwd=str(isolated_cwd), keep_thinking=True))
    _record_forge_case(
        report, case_id='2A', name='signed_thinking_preserve',
        source_path=src2, forged_events=f2a.events, new_session_id=sid2a,
        work_root=work_root, isolated_cwd=isolated_cwd, claude_home=claude_home, run_resume=live,
    )
    # CASE 2B drop thinking
    sid2b = new_uuid()
    f2b = forge_transcript(
        ev2,
        ForgeOptions(new_session_id=sid2b, cwd=str(isolated_cwd), keep_thinking=False, drop_thinking=True),
    )
    _record_forge_case(
        report, case_id='2B', name='signed_thinking_drop_block',
        source_path=src2, forged_events=f2b.events, new_session_id=sid2b,
        work_root=work_root, isolated_cwd=isolated_cwd, claude_home=claude_home, run_resume=live,
    )

    # CASE 3 tool success (structural + optional live)
    src3 = fixture_root / 'case3_tool_success.jsonl'
    sid3 = new_uuid()
    f3 = forge_transcript(load_jsonl(src3), ForgeOptions(new_session_id=sid3, cwd=str(isolated_cwd)))
    _record_forge_case(
        report, case_id='3A', name='tool_round_success',
        source_path=src3, forged_events=f3.events, new_session_id=sid3,
        work_root=work_root, isolated_cwd=isolated_cwd, claude_home=claude_home, run_resume=live,
    )

    # CASE 5 sidechain exclude
    src5 = fixture_root / 'case5_sidechain.jsonl'
    sid5 = new_uuid()
    f5 = forge_transcript(
        load_jsonl(src5),
        ForgeOptions(new_session_id=sid5, cwd=str(isolated_cwd), exclude_sidechain=True),
    )
    _record_forge_case(
        report, case_id='5B', name='sidechain_exclude',
        source_path=src5, forged_events=f5.events, new_session_id=sid5,
        work_root=work_root, isolated_cwd=isolated_cwd, claude_home=claude_home, run_resume=live,
    )

    # CASE 6 summary + uuid refs
    src6 = fixture_root / 'case6_summary_uuid_refs.jsonl'
    sid6 = new_uuid()
    f6 = forge_transcript(load_jsonl(src6), ForgeOptions(new_session_id=sid6, cwd=str(isolated_cwd)))
    _record_forge_case(
        report, case_id='6', name='summary_drop_uuid_scan',
        source_path=src6, forged_events=f6.events, new_session_id=sid6,
        work_root=work_root, isolated_cwd=isolated_cwd, claude_home=claude_home,
        old_uuids={str(e.get('uuid')) for e in load_jsonl(src6) if e.get('uuid')},
        run_resume=live,
    )

    # CASE 7 legacy injection strip
    src7 = fixture_root / 'case7_legacy_injection.jsonl'
    canon = json.loads((fixture_root / 'app_db_canonical_messages.json').read_text(encoding='utf-8'))
    ev7 = load_jsonl(src7)
    inj_uuid = next(str(e.get('uuid')) for e in ev7 if e.get('type') == 'user')
    sid7 = new_uuid()
    f7 = forge_transcript(
        ev7,
        ForgeOptions(
            new_session_id=sid7,
            cwd=str(isolated_cwd),
            user_canonical_by_event_uuid={inj_uuid: canon['by_claude_event_uuid'][inj_uuid]},
        ),
    )
    _record_forge_case(
        report, case_id='7', name='legacy_injection_strip',
        source_path=src7, forged_events=f7.events, new_session_id=sid7,
        work_root=work_root, isolated_cwd=isolated_cwd, claude_home=claude_home, run_resume=live,
    )

    # CASE 8 negative fixtures (structure only)
    for neg_id, label, events in (
        ('8-empty-thinking', 'empty_thinking', [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': new_uuid(),
             'message': {'role': 'user', 'content': 'x'}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': new_uuid(),
             'message': {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': ''}]}},
        ]),
        ('8-orphan-tool', 'orphan_tool_use', [
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
        for e in events:
            e['sessionId'] = sid8
        v = validate_forged_transcript(events, session_id=sid8)
        c8.structure_ok = v.ok
        c8.state = 'STRUCTURE_REJECT' if not v.ok else 'UNEXPECTED_PASS'
        c8.error_detail = redact(';'.join(v.errors[:3]))
        report.cases.append(c8)

    _decide_verdict(report)
    return report


def _decide_verdict(report: SpikeReport) -> None:
    if not report.auth_available:
        report.verdict = 'NO-GO'
        report.verdict_reason = (
            '缺少隔离试验 API 凭证：云 Agent 环境无 ANTHROPIC_API_KEY / '
            'CLAUDE_CODE_OAUTH_TOKEN，无法完成 CASE 0–7 首次真实 resume 硬门。'
        )
        return

    c1 = next((c for c in report.cases if c.case_id == '1'), None)
    c0 = next((c for c in report.cases if c.case_id == '0'), None)
    if not c0 or c0.state != 'API_ACCEPTED_FIRST_DELTA':
        report.verdict = 'NO-GO'
        report.verdict_reason = 'CASE 0 控制组未通过，试验环境不可用'
        return
    if not c1 or c1.state != 'API_ACCEPTED_FIRST_DELTA':
        report.verdict = 'NO-GO'
        report.verdict_reason = 'CASE 1 最小纯文本 Forge 未通过首次真实 resume'
        return

    c2b = next((c for c in report.cases if c.case_id == '2B'), None)
    c2a = next((c for c in report.cases if c.case_id == '2A'), None)
    if c2a and c2a.state != 'API_ACCEPTED_FIRST_DELTA' and c2b and c2b.state == 'API_ACCEPTED_FIRST_DELTA':
        report.verdict = 'CONDITIONAL GO'
        report.verdict_reason = 'signed thinking 原样保留失败，但整块删除后 resume 成功'
        return

    report.verdict = 'GO'
    report.verdict_reason = '控制组与最小 Forge 均通过首次真实 resume'


def main() -> int:
    parser = argparse.ArgumentParser(description='Claude forge resume spike harness')
    parser.add_argument('--work-root', type=Path, default=None)
    parser.add_argument('--report', type=Path, default=ROOT / 'artifacts' / 'spike-claude-forge-resume' / 'results.json')
    parser.add_argument('--structural-only', action='store_true', help='Skip live resume probes')
    args = parser.parse_args()

    report = run_spike(args.work_root, live=not args.structural_only)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.verdict.startswith('GO') else 2


if __name__ == '__main__':
    raise SystemExit(main())
