"""Live resume gate helpers for Claude forge spike (testable without API)."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from tools.claude_forge_core import UUID_RE, _content_blocks, _message_text, new_uuid, scan_unknown_uuid_strings
from tools.claude_forge_validator import validate_forged_transcript

SYSTEM_PROMPT = '你是隔离 Spike 测试助手。只回复简短确认，不要调用工具。'
CONVERSATIONAL_EVENT_TYPES = frozenset({'user', 'assistant'})
RAW_CONVERSATION_NODE = 'conversation_node'
RAW_ASSISTANT_USAGE_OBSERVATION = 'assistant_usage_observation'
RAW_METADATA = 'metadata'
RAW_MALFORMED_CONVERSATION_EVENT = 'malformed_conversation_event'
KNOWN_METADATA_TYPES = frozenset({
    'file-history-snapshot',
    'queue-operation',
    'agent-name',
    'custom-title',
    'progress',
})


def generate_history_canary() -> str:
    return f'CANARY-{secrets.token_hex(16)}'


def build_live_user_prompt() -> str:
    return '请只回复上一轮助手消息末尾约定的历史校验码，不要添加任何其他字符。'


def inject_canary_into_history(events: list[dict[str, Any]], canary: str) -> list[dict[str, Any]]:
    if not events:
        return events
    out = [json.loads(json.dumps(e)) for e in events]
    leaf = next((evt for evt in reversed(out) if _is_conversational_event(evt)), None)
    if leaf is None or leaf.get('type') != 'assistant':
        raise ValueError('conversation leaf is not assistant')
    message = leaf.setdefault('message', {})
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
    message['content'] = [{'type': 'text', 'text': f'[history-canary:{canary}]'}]
    return out


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
    leaf = next((evt for evt in reversed(events) if _is_conversational_event(evt)), None)
    if leaf is None:
        raise ValueError('history has no conversational event')
    if leaf.get('type') == 'assistant':
        return inject_canary_into_history(events, canary)
    out = [json.loads(json.dumps(e)) for e in events]
    parent = str(leaf.get('uuid') or '')
    if not parent:
        raise ValueError('conversation leaf missing uuid')
    a_id = new_uuid()
    out.append({
        'type': 'assistant',
        'uuid': a_id,
        'parentUuid': parent,
        'timestamp': leaf.get('timestamp') or '2026-07-30T00:00:00.000Z',
        'sessionId': session_id,
        'cwd': cwd,
        'version': 'managed-runtime-spike',
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
    raw_append_valid: bool = False
    append_valid: bool = False
    warnings: list[str] = field(default_factory=list)
    raw: Optional[LiveProbeRaw] = None


@dataclass(frozen=True)
class RawEventClassification:
    kind: str
    reason: str = ''
    request_id: str = ''


def _has_message_body(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            (isinstance(block, str) and bool(block.strip()))
            or (isinstance(block, Mapping) and bool(block))
            for block in content
        )
    return content is not None


def classify_raw_jsonl_event(evt: dict[str, Any]) -> RawEventClassification:
    """Classify one raw Claude JSONL object without weakening conversation validation."""
    etype = evt.get('type')
    if etype not in CONVERSATIONAL_EVENT_TYPES:
        return RawEventClassification(RAW_METADATA)

    message = evt.get('message')
    if not isinstance(message, dict):
        return RawEventClassification(RAW_MALFORMED_CONVERSATION_EVENT, 'message_missing')
    if message.get('role') != etype:
        return RawEventClassification(RAW_MALFORMED_CONVERSATION_EVENT, 'role_mismatch')

    uid = str(evt.get('uuid') or '')
    uuid_ok = bool(uid and UUID_RE.match(uid))
    session_ok = bool(str(evt.get('sessionId') or ''))
    content_present = 'content' in message
    body_present = content_present and _has_message_body(message.get('content'))
    canonical = uuid_ok and session_ok and content_present and (etype == 'user' or body_present)
    if canonical:
        return RawEventClassification(RAW_CONVERSATION_NODE)

    request_id = str(evt.get('requestId') or evt.get('request_id') or '').strip()
    usage = message.get('usage')
    if (
        etype == 'assistant'
        and request_id
        and isinstance(usage, Mapping)
        and (not canonical or not body_present)
    ):
        return RawEventClassification(
            RAW_ASSISTANT_USAGE_OBSERVATION,
            request_id=request_id,
        )

    if not uuid_ok:
        reason = 'uuid_missing_or_invalid'
    elif not session_ok:
        reason = 'session_id_missing'
    elif not content_present:
        reason = 'content_missing'
    elif etype == 'assistant' and not body_present:
        reason = 'content_empty'
    else:
        reason = 'incomplete_conversation_node'
    return RawEventClassification(RAW_MALFORMED_CONVERSATION_EVENT, reason)


def _is_conversational_event(evt: dict[str, Any]) -> bool:
    return classify_raw_jsonl_event(evt).kind == RAW_CONVERSATION_NODE


def project_conversational_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project raw Claude JSONL onto user/assistant events only."""
    return [evt for evt in events if _is_conversational_event(evt)]


def _metadata_kind(evt: dict[str, Any]) -> str:
    etype = str(evt.get('type') or '')
    if etype in KNOWN_METADATA_TYPES:
        return etype
    if etype == 'system' and str(evt.get('subtype') or '') == 'turn_duration':
        return 'system/turn_duration'
    return etype or '<missing>'


def _classify_raw_events(
    events: list[dict[str, Any]],
    *,
    warning_sink: list[str],
) -> list[str]:
    failures: list[str] = []
    usage_by_request: dict[str, str] = {}
    for offset, evt in enumerate(events):
        classification = classify_raw_jsonl_event(evt)
        if classification.kind == RAW_MALFORMED_CONVERSATION_EVENT:
            failures.append(
                f'malformed_conversation_event:{offset}:{classification.reason}'
            )
            continue
        if classification.kind != RAW_ASSISTANT_USAGE_OBSERVATION:
            continue
        usage = (evt.get('message') or {}).get('usage')
        signature = json.dumps(usage, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        request_id = classification.request_id
        if request_id in usage_by_request:
            if usage_by_request[request_id] == signature:
                warning_sink.append(f'duplicate_assistant_usage_request_id:{request_id}')
            else:
                warning_sink.append(f'conflicting_assistant_usage_request_id:{request_id}')
        else:
            usage_by_request[request_id] = signature
    return failures


def parse_raw_jsonl_append(
    before: bytes,
    after: bytes,
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """Validate the byte prefix and parse only newly appended JSONL objects."""
    failures: list[str] = []
    if not verify_jsonl_prefix_unchanged(before, after):
        return False, [], ['jsonl_prefix_changed']
    suffix = after[len(before):]
    if not suffix:
        return False, [], ['append_empty']
    try:
        text = suffix.decode('utf-8')
    except UnicodeDecodeError:
        return False, [], ['append_invalid_utf8']
    new_events: list[dict[str, Any]] = []
    for offset, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            failures.append(f'append_invalid_json:{offset}')
            continue
        if not isinstance(evt, dict):
            failures.append(f'append_not_object:{offset}')
            continue
        new_events.append(evt)
    if not new_events:
        failures.append('append_no_json_objects')
    return not failures, new_events, failures


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
    warning_sink: Optional[list[str]] = None,
) -> tuple[bool, str, list[str]]:
    failures: list[str] = []
    warnings = warning_sink if warning_sink is not None else []
    if len(after_events) < len(before_events):
        return False, 'append_too_few_events', failures
    if after_events[:len(before_events)] != before_events:
        return False, 'prefix_events_changed', failures

    before_conversation = project_conversational_events(before_events)
    after_conversation = project_conversational_events(after_events)
    failures.extend(_classify_raw_events(after_events, warning_sink=warnings))
    if not before_conversation:
        return False, 'missing_before_conversation', failures
    if after_conversation[:len(before_conversation)] != before_conversation:
        return False, 'conversation_prefix_changed', failures

    old_leaf = str(before_conversation[-1].get('uuid') or '')
    new_conversation = after_conversation[len(before_conversation):]
    if len(new_conversation) < 2:
        return False, 'append_too_few_conversation_events', failures

    all_conversation = before_conversation + new_conversation
    known_uuids: set[str] = set()
    for idx, evt in enumerate(all_conversation):
        uid = str(evt.get('uuid') or '')
        if not uid or not UUID_RE.match(uid):
            failures.append(f'append_bad_uuid:{idx}')
            continue
        if uid in known_uuids:
            failures.append(f'append_duplicate_uuid:{uid}')
        known_uuids.add(uid)

    prev_uuid = old_leaf
    matching_users: list[dict[str, Any]] = []
    for idx, evt in enumerate(new_conversation):
        uid = str(evt.get('uuid') or '')
        if str(evt.get('sessionId') or '') != expected_session_id:
            failures.append(f'append_bad_session_id:{uid}')
        if str(evt.get('parentUuid') or '') != prev_uuid:
            failures.append(f'append_bad_parent:{uid}')
        etype = evt.get('type')
        if etype == 'user':
            if _message_text((evt.get('message') or {}).get('content')) == live_user_prompt:
                matching_users.append(evt)
        prev_uuid = uid

    if not matching_users:
        failures.append('append_missing_user')
        live_user: Optional[dict[str, Any]] = None
    else:
        live_user = matching_users[0]
        if len(matching_users) > 1:
            failures.append('append_duplicate_live_user')
        if str(live_user.get('parentUuid') or '') != old_leaf:
            failures.append('append_user_not_connected_to_old_leaf')

    matching_assistants = [
        evt for evt in new_conversation
        if evt.get('type') == 'assistant'
        and live_user is not None
        and str(evt.get('parentUuid') or '') == str(live_user.get('uuid') or '')
    ]
    if not matching_assistants:
        failures.append('append_missing_assistant')
    else:
        live_assistant = matching_assistants[0]
        if _assistant_on_disk_text(live_assistant) != canary.strip():
            failures.append('append_assistant_canary_mismatch')
        if str(live_assistant.get('sessionId') or '') != expected_session_id:
            failures.append(f'append_bad_session_id:{live_assistant.get("uuid") or ""}')

    for evt in after_events[len(before_events):]:
        classification = classify_raw_jsonl_event(evt)
        if classification.kind != RAW_METADATA:
            continue
        kind = _metadata_kind(evt)
        if kind not in KNOWN_METADATA_TYPES and kind != 'system/turn_duration':
            warnings.append(f'unknown_metadata_type:{kind}')

    if old_uuids:
        for evt in after_events:
            failures.extend(scan_unknown_uuid_strings(evt, old_uuids))

    validation = validate_forged_transcript(
        after_conversation,
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

    raw_ok, _, raw_failures = parse_raw_jsonl_append(before_bytes, after_bytes)
    result.raw_append_valid = raw_ok
    if not raw_ok:
        failures.extend(raw_failures)

    warnings: list[str] = []
    append_ok, append_reason, append_failures = verify_jsonl_append_chain(
        before_events,
        after_events,
        expected_session_id=expected_session_id,
        live_user_prompt=build_live_user_prompt(),
        canary=canary,
        old_uuids=old_uuids,
        warning_sink=warnings,
    )
    result.append_valid = append_ok
    if not append_ok:
        failures.append(append_reason or 'append_invalid')
        failures.extend(append_failures)

    result.failures = list(dict.fromkeys(failures))
    result.warnings = list(dict.fromkeys(warnings))
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
