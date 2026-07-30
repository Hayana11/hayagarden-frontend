"""Live resume gate helpers for Claude forge spike (testable without API)."""
from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from tools.claude_forge_core import UUID_RE, _content_blocks, _message_text, load_jsonl
from pathlib import Path

CLAUDE_CODE_PINNED_VERSION = '2.1.220'
CLAUDE_CODE_NPM_SPEC = f'@anthropic-ai/claude-code@{CLAUDE_CODE_PINNED_VERSION}'

SYSTEM_PROMPT = '你是隔离 Spike 测试助手。只回复简短确认，不要调用工具。'


def generate_history_canary() -> str:
    """Unpredictable token embedded only in forged history, never in live prompts."""
    return f'CANARY-{secrets.token_hex(16)}'


def build_live_user_prompt() -> str:
    """Probe prompt must not contain the canary."""
    return '请只回复上一轮助手消息末尾约定的历史校验码，不要添加任何其他字符。'


def inject_canary_into_history(events: list[dict[str, Any]], canary: str) -> list[dict[str, Any]]:
    """Append canary to the last assistant text block in forged history."""
    if not events:
        return events
    out = [json.loads(json.dumps(e)) for e in events]
    for evt in reversed(out):
        if evt.get('type') != 'assistant':
            continue
        message = evt.setdefault('message', {})
        content = message.get('content')
        if isinstance(content, str):
            message['content'] = content.rstrip() + f'\n[history-canary:{canary}]'
            return out
        if isinstance(content, list):
            for block in reversed(content):
                if isinstance(block, dict) and block.get('type') == 'text':
                    block['text'] = str(block.get('text') or '').rstrip() + f'\n[history-canary:{canary}]'
                    return out
            content.append({'type': 'text', 'text': f'[history-canary:{canary}]'})
            return out
    raise ValueError('no assistant event to inject canary')


@dataclass
class LiveProbeRaw:
    process_started: bool = False
    exit_code: Optional[int] = None
    assistant_text: str = ''
    result_ok: bool = False
    result_is_error: Optional[bool] = None
    stdout_session_id: str = ''
    stdout_lines: list[str] = field(default_factory=list)
    stderr_text: str = ''
    error_type: str = ''
    error_detail: str = ''
    timed_out: bool = False


@dataclass
class LiveGateResult:
    passed: bool = False
    state: str = 'NOT_RUN'
    failures: list[str] = field(default_factory=list)
    canary: str = ''
    canary_matched: bool = False
    prefix_unchanged: bool = False
    append_valid: bool = False
    raw: Optional[LiveProbeRaw] = None


def _extract_assistant_text(evt: dict[str, Any]) -> str:
    message = evt.get('message') or {}
    content = message.get('content')
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in _content_blocks(message):
        if block.get('type') == 'text':
            parts.append(str(block.get('text') or ''))
    return ''.join(parts)


def parse_stdout_events(lines: list[str]) -> LiveProbeRaw:
    raw = LiveProbeRaw(process_started=True)
    text_parts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        raw.stdout_lines.append(line)
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        sid = evt.get('session_id') or evt.get('sessionId')
        if sid:
            raw.stdout_session_id = str(sid)
        etype = evt.get('type')
        if etype == 'result':
            raw.result_ok = not bool(evt.get('is_error'))
            raw.result_is_error = bool(evt.get('is_error'))
        delta = evt.get('delta')
        if isinstance(delta, dict) and delta.get('type') == 'text_delta':
            text_parts.append(str(delta.get('text') or ''))
        text_parts.append(_extract_assistant_text(evt))
    raw.assistant_text = ''.join(text_parts).strip()
    return raw


def verify_jsonl_prefix_unchanged(before: bytes, after: bytes) -> bool:
    if len(after) < len(before):
        return False
    return after[: len(before)] == before


def verify_jsonl_append_chain(
    before_events: list[dict[str, Any]],
    after_events: list[dict[str, Any]],
) -> tuple[bool, str]:
    if len(after_events) < len(before_events) + 2:
        return False, 'append_too_few_events'
    if after_events[: len(before_events)] != before_events:
        return False, 'prefix_events_changed'
    old_leaf = str(before_events[-1].get('uuid') or '')
    new_user = after_events[len(before_events)]
    new_assistant = after_events[len(before_events) + 1]
    if new_user.get('type') != 'user':
        return False, 'append_first_not_user'
    if new_assistant.get('type') != 'assistant':
        return False, 'append_second_not_assistant'
    if str(new_user.get('parentUuid') or '') != old_leaf:
        return False, 'append_user_parent_not_old_leaf'
    if str(new_assistant.get('parentUuid') or '') != str(new_user.get('uuid') or ''):
        return False, 'append_assistant_parent_not_new_user'
    return True, ''


def evaluate_live_gate(
    *,
    raw: LiveProbeRaw,
    expected_session_id: str,
    canary: str,
    before_bytes: bytes,
    after_bytes: bytes,
    before_events: list[dict[str, Any]],
    after_events: list[dict[str, Any]],
) -> LiveGateResult:
    result = LiveGateResult(raw=raw, canary=canary)
    failures: list[str] = []

    if not raw.process_started:
        failures.append('process_not_started')
    if raw.exit_code != 0:
        failures.append(f'exit_code:{raw.exit_code}')
    if not raw.assistant_text:
        failures.append('missing_assistant_text')
    if not raw.result_ok:
        failures.append('result_not_ok')
    if raw.result_is_error is not False:
        failures.append('result_is_error_not_false')
    if not raw.stdout_session_id:
        failures.append('missing_stdout_session_id')
    elif raw.stdout_session_id != expected_session_id:
        failures.append('session_id_mismatch')

    result.canary_matched = raw.assistant_text.strip() == canary.strip()
    if not result.canary_matched:
        failures.append('canary_mismatch')

    result.prefix_unchanged = verify_jsonl_prefix_unchanged(before_bytes, after_bytes)
    if not result.prefix_unchanged:
        failures.append('jsonl_prefix_changed')

    append_ok, append_reason = verify_jsonl_append_chain(before_events, after_events)
    result.append_valid = append_ok
    if not append_ok:
        failures.append(append_reason or 'append_invalid')

    result.failures = failures
    result.passed = len(failures) == 0
    result.state = 'API_ACCEPTED_FIRST_DELTA' if result.passed else 'RESUME_FAIL'
    return result


def run_subprocess_with_timeout(
    *,
    popen_factory: Callable[[], Any],
    stdin_payload: str,
    timeout_seconds: float,
    on_line: Callable[[str], None],
) -> tuple[int, bool]:
    """Run subprocess with non-blocking stdout read; kill on timeout."""
    proc = popen_factory()
    timed_out = False
    lines: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            on_line(line)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(stdin_payload)
        proc.stdin.flush()
        proc.stdin.close()
        reader.join(timeout=timeout_seconds)
        if reader.is_alive():
            timed_out = True
            proc.kill()
            reader.join(timeout=5)
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        raise
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    for line in lines:
        on_line(line)
    return int(proc.returncode or 0), timed_out


def decide_verdict(
    cases: list[dict[str, Any]],
    *,
    structural_only: bool,
    live_probe_status: str,
) -> tuple[str, str]:
    """Return (verdict, reason). Never GO without all required live cases passing."""
    if structural_only or live_probe_status == 'NOT_RUN_NO_CREDENTIALS':
        return (
            'NO-GO',
            'live probe NOT_RUN_NO_CREDENTIALS — structural validation only; '
            'cannot upgrade to GO without strict live gate evidence.',
        )

    required = ('0', '1', '2A', '2B', '3A', '5B', '6', '7')
    by_id = {c['case_id']: c for c in cases}

    def _state(cid: str) -> str:
        return str(by_id.get(cid, {}).get('state') or 'MISSING')

    def _passed(cid: str) -> bool:
        return _state(cid) == 'API_ACCEPTED_FIRST_DELTA'

    if not _passed('0') or not _passed('1'):
        return 'NO-GO', 'CASE 0 或 CASE 1 live gate 未通过'

    c2a, c2b = _passed('2A'), _passed('2B')
    if not c2a and not c2b:
        return 'NO-GO', 'CASE 2A 与 2B 均失败 — 不得 GO 或仅凭结构通过升级'

    missing_or_failed = [cid for cid in required if not _passed(cid)]
    if not missing_or_failed:
        return 'GO', '全部必需 live CASE 通过严格 gate'

    if _passed('0') and _passed('1'):
        return (
            'CONDITIONAL GO',
            f'CASE 0/1 通过，但其他必需 live CASE 未通过: {", ".join(missing_or_failed)}',
        )
    return 'NO-GO', f'必需 live CASE 未通过: {", ".join(missing_or_failed)}'
