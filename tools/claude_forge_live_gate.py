"""Live resume gate helpers for Claude forge spike (testable without API)."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Optional

from tools.claude_forge_core import UUID_RE, _content_blocks, _message_text, new_uuid, scan_unknown_uuid_strings
from tools.claude_forge_validator import validate_forged_transcript

CLAUDE_CODE_PINNED_VERSION = '2.1.220'
CLAUDE_CODE_NPM_SPEC = f'@anthropic-ai/claude-code@{CLAUDE_CODE_PINNED_VERSION}'

SYSTEM_PROMPT = '你是隔离 Spike 测试助手。只回复简短确认，不要调用工具。'


def generate_history_canary() -> str:
    return f'CANARY-{secrets.token_hex(16)}'


def build_live_user_prompt() -> str:
    return '请只回复上一轮助手消息末尾约定的历史校验码，不要添加任何其他字符。'


def inject_canary_into_history(events: list[dict[str, Any]], canary: str) -> list[dict[str, Any]]:
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


def prepare_history_for_live_gate(
    events: list[dict[str, Any]],
    canary: str,
    *,
    session_id: str,
    cwd: str,
) -> list[dict[str, Any]]:
    """Ensure history ends with an assistant carrying the canary (user-tail safe)."""
    if not events:
        raise ValueError('empty history')
    if any(evt.get('type') == 'assistant' for evt in events):
        return inject_canary_into_history(events, canary)
    out = [json.loads(json.dumps(e)) for e in events]
    parent = str(out[-1].get('uuid') or '')
    if not parent:
        raise ValueError('tail event missing uuid')
    a_id = new_uuid()
    out.append({
        'type': 'assistant',
        'uuid': a_id,
        'parentUuid': parent,
        'timestamp': out[-1].get('timestamp') or '2026-07-30T00:00:00.000Z',
        'sessionId': session_id,
        'cwd': cwd,
        'version': f'{CLAUDE_CODE_PINNED_VERSION}-spike',
        'message': {
            'role': 'assistant',
            'content': [{'type': 'text', 'text': f'[history-canary:{canary}]'}],
        },
    })
    return out


@dataclass
class LiveProbeRaw:
    process_started: bool = False
    exit_code: Optional[int] = None
    assistant_text: str = ''
    saw_text_delta: bool = False
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


def _extract_assistant_message_text(evt: dict[str, Any]) -> str:
    if evt.get('type') != 'assistant':
        return ''
    return _message_text((evt.get('message') or {}).get('content'))


def _extract_stream_text_delta(evt: dict[str, Any]) -> str:
    if evt.get('type') != 'stream_event':
        return ''
    inner = evt.get('event')
    if not isinstance(inner, dict):
        return ''
    delta = inner.get('delta')
    if isinstance(delta, dict) and delta.get('type') == 'text_delta':
        return str(delta.get('text') or '')
    block = inner.get('content_block_delta')
    if isinstance(block, dict):
        nested = block.get('delta')
        if isinstance(nested, dict) and nested.get('type') == 'text_delta':
            return str(nested.get('text') or '')
    return ''


def parse_stdout_events(lines: list[str]) -> LiveProbeRaw:
    raw = LiveProbeRaw(process_started=True)
    delta_parts: list[str] = []
    assistant_parts: list[str] = []
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
        stream_delta = _extract_stream_text_delta(evt)
        if stream_delta:
            raw.saw_text_delta = True
            delta_parts.append(stream_delta)
        assistant_text = _extract_assistant_message_text(evt)
        if assistant_text:
            assistant_parts.append(assistant_text)
    if assistant_parts:
        raw.assistant_text = assistant_parts[-1].strip()
    elif delta_parts:
        raw.assistant_text = ''.join(delta_parts).strip()
    return raw


def verify_jsonl_prefix_unchanged(before: bytes, after: bytes) -> bool:
    if len(after) < len(before):
        return False
    return after[: len(before)] == before


def _assistant_on_disk_text(evt: dict[str, Any]) -> str:
    return _message_text((evt.get('message') or {}).get('content')).strip()


def verify_jsonl_append_chain(
    before_events: list[dict[str, Any]],
    after_events: list[dict[str, Any]],
    *,
    expected_session_id: str,
    live_user_prompt: str,
    canary: str,
    old_uuids: Optional[set[str]] = None,
) -> tuple[bool, str, list[str]]:
    failures: list[str] = []
    n_before = len(before_events)
    if len(after_events) < n_before + 2:
        return False, 'append_too_few_events', failures
    if after_events[:n_before] != before_events:
        return False, 'prefix_events_changed', failures

    old_leaf = str(before_events[-1].get('uuid') or '')
    known_uuids = {str(e.get('uuid') or '') for e in before_events if e.get('uuid')}
    new_events = after_events[n_before:]

    prev_uuid = old_leaf
    saw_user = False
    saw_assistant = False
    for idx, evt in enumerate(new_events):
        uid = str(evt.get('uuid') or '')
        if not uid or not UUID_RE.match(uid):
            failures.append(f'append_bad_uuid:{n_before + idx}')
            continue
        if uid in known_uuids:
            failures.append(f'append_duplicate_uuid:{uid}')
        known_uuids.add(uid)
        if str(evt.get('sessionId') or '') != expected_session_id:
            failures.append(f'append_bad_session_id:{uid}')
        if str(evt.get('parentUuid') or '') != prev_uuid:
            failures.append(f'append_bad_parent:{uid}')
        etype = evt.get('type')
        if etype == 'user':
            saw_user = True
            if _message_text((evt.get('message') or {}).get('content')) != live_user_prompt:
                failures.append('append_user_prompt_mismatch')
        elif etype == 'assistant':
            saw_assistant = True
            if _assistant_on_disk_text(evt) != canary.strip():
                failures.append('append_assistant_canary_mismatch')
        prev_uuid = uid

    if not saw_user:
        failures.append('append_missing_user')
    if not saw_assistant:
        failures.append('append_missing_assistant')

    if old_uuids:
        for evt in after_events:
            failures.extend(scan_unknown_uuid_strings(evt, old_uuids))

    validation = validate_forged_transcript(
        after_events,
        session_id=expected_session_id,
        old_uuids=old_uuids,
    )
    if not validation.ok:
        failures.extend(validation.errors[:5])

    if failures:
        return False, failures[0], failures
    return True, '', failures


def evaluate_live_gate(
    *,
    raw: LiveProbeRaw,
    expected_session_id: str,
    canary: str,
    before_bytes: bytes,
    after_bytes: bytes,
    before_events: list[dict[str, Any]],
    after_events: list[dict[str, Any]],
    old_uuids: Optional[set[str]] = None,
) -> LiveGateResult:
    result = LiveGateResult(raw=raw, canary=canary)
    failures: list[str] = []

    if not raw.process_started:
        failures.append('process_not_started')
    if raw.exit_code != 0:
        failures.append(f'exit_code:{raw.exit_code}')
    if not raw.saw_text_delta:
        failures.append('missing_text_delta')
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

    append_ok, append_reason, append_failures = verify_jsonl_append_chain(
        before_events,
        after_events,
        expected_session_id=expected_session_id,
        live_user_prompt=build_live_user_prompt(),
        canary=canary,
        old_uuids=old_uuids,
    )
    result.append_valid = append_ok
    if not append_ok:
        failures.append(append_reason or 'append_invalid')
        failures.extend(append_failures)

    result.failures = list(dict.fromkeys(failures))
    result.passed = len(result.failures) == 0
    result.state = 'API_ACCEPTED_FIRST_DELTA' if result.passed else 'RESUME_FAIL'
    return result


def decide_verdict(
    cases: list[dict[str, Any]],
    *,
    structural_only: bool,
    live_probe_status: str,
) -> tuple[str, str]:
    if structural_only or live_probe_status == 'NOT_RUN_NO_CREDENTIALS':
        return (
            'NO-GO',
            'live probe NOT_RUN_NO_CREDENTIALS — structural validation only; '
            'cannot upgrade to GO without strict live gate evidence.',
        )

    required = ('0', '1', '2A', '2B', '3A', '5B', '6', '7')
    by_id = {c['case_id']: c for c in cases}

    def _passed(cid: str) -> bool:
        return str(by_id.get(cid, {}).get('state') or 'MISSING') == 'API_ACCEPTED_FIRST_DELTA'

    if not _passed('0') or not _passed('1'):
        return 'NO-GO', 'CASE 0 或 CASE 1 live gate 未通过'

    if not _passed('2A') and not _passed('2B'):
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
