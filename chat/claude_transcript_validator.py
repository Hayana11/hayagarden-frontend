"""Structural validator for transformed Claude transcripts (v0.2 Transcript Core).

Proves only that local structure matches the contract.
Does NOT claim Claude resume acceptance, Forge success, or server-side validation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from chat.claude_transcript_model import ThinkingPolicy

UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
# UUID-shaped token inside arbitrary text (for residual scans)
UUID_TOKEN_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)
TOOL_ID_RE = re.compile(r'^toolu_[A-Za-z0-9]+$')

# Paths where UUID strings are structural references (must be remapped / known)
UUID_REFERENCE_PATH_SUFFIXES = frozenset({
    'uuid',
    'parentUuid',
    'leafUuid',
    'sourceToolAssistantUUID',
    'agentId',
})

ALLOWED_EVENT_TYPES = frozenset({'user', 'assistant', 'system'})


class ValidatorErrorCode(str, Enum):
    STRUCTURE = 'VALIDATOR_STRUCTURE'
    SESSION = 'VALIDATOR_SESSION'
    UUID = 'VALIDATOR_UUID'
    PARENT = 'VALIDATOR_PARENT'
    SUMMARY = 'VALIDATOR_SUMMARY'
    SIDECHAIN = 'VALIDATOR_SIDECHAIN'
    THINKING = 'VALIDATOR_THINKING'
    TOOL_PAIR = 'VALIDATOR_TOOL_PAIR'
    TOOL_ID = 'VALIDATOR_TOOL_ID'
    ROUND_COUNT = 'VALIDATOR_ROUND_COUNT'
    UNKNOWN_EVENT = 'VALIDATOR_UNKNOWN_EVENT'
    BUDGET = 'VALIDATOR_BUDGET'
    OLD_UUID_RESIDUAL = 'VALIDATOR_OLD_UUID_RESIDUAL'
    JSON_LINE = 'VALIDATOR_JSON_LINE'


@dataclass
class ValidationResult:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    # Explicit: structural pass only — never "resume accepted"
    proof_kind: str = 'local_structure_contract'

    def add(self, code: ValidatorErrorCode, detail: str = '') -> None:
        msg = code.value if not detail else f'{code.value}:{detail}'
        self.errors.append(msg)
        self.ok = False


@dataclass(frozen=True)
class ValidatorOptions:
    session_id: str
    thinking_policy: ThinkingPolicy = ThinkingPolicy.DROP
    forbid_sidechain: bool = True
    forbid_summary: bool = True
    expected_round_count: Optional[int] = None
    max_round_count: Optional[int] = None
    old_uuids: Optional[set[str]] = None
    # path suffixes where UUID tokens are allowed without being old residuals
    uuid_path_allowlist: frozenset[str] = frozenset({
        'uuid',
        'parentUuid',
        'sessionId',
        # plain message text may coincidentally contain UUID-shaped strings
        'content',
        'text',
        'thinking',
    })
    max_output_bytes: Optional[int] = None
    unknown_event_mode: str = 'reject'  # reject | warn


def _content_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get('content')
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _is_tool_result_only_user(message: Mapping[str, Any]) -> bool:
    content = message.get('content')
    if not isinstance(content, list) or not content:
        return False
    blocks = [b for b in content if isinstance(b, dict)]
    if not blocks:
        return False
    return all(b.get('type') == 'tool_result' for b in blocks)


def _count_real_rounds(events: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for evt in events:
        if evt.get('type') != 'user':
            continue
        message = evt.get('message') if isinstance(evt.get('message'), dict) else {}
        if _is_tool_result_only_user(message):
            continue
        count += 1
    return count


def _walk_uuid_tokens(
    obj: Any,
    *,
    path: str,
    old_uuids: set[str],
    allow_suffixes: frozenset[str],
    hits: list[str],
) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            child = f'{path}.{key}' if path else key
            _walk_uuid_tokens(
                val,
                path=child,
                old_uuids=old_uuids,
                allow_suffixes=allow_suffixes,
                hits=hits,
            )
        return
    if isinstance(obj, list):
        for idx, val in enumerate(obj):
            _walk_uuid_tokens(
                val,
                path=f'{path}[{idx}]',
                old_uuids=old_uuids,
                allow_suffixes=allow_suffixes,
                hits=hits,
            )
        return
    if not isinstance(obj, str):
        return

    leaf = path.rsplit('.', 1)[-1]
    leaf = leaf.split('[', 1)[0]
    # Exact UUID field
    if UUID_RE.match(obj):
        if obj in old_uuids and leaf not in allow_suffixes:
            hits.append(f'{path}={obj}')
        elif obj in old_uuids and leaf in UUID_REFERENCE_PATH_SUFFIXES:
            # structural refs must not retain old UUIDs even if suffix allowlisted
            # except sessionId-like non-event fields already handled by allowlist
            if leaf in {'uuid', 'parentUuid', 'leafUuid', 'sourceToolAssistantUUID', 'agentId'}:
                hits.append(f'{path}={obj}')
        return

    # Embedded UUID tokens in non-allowlisted paths
    if leaf in allow_suffixes:
        # ordinary chat text may contain UUID-shaped strings; do not treat as refs
        return
    for match in UUID_TOKEN_RE.findall(obj):
        if match in old_uuids:
            hits.append(f'{path}~{match}')


def events_are_json_serializable(events: Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    for idx, evt in enumerate(events, 1):
        try:
            json.dumps(evt, ensure_ascii=False, separators=(',', ':'))
        except (TypeError, ValueError) as exc:
            errors.append(f'line_{idx}:{exc}')
    return errors


def validate_transcript_events(
    events: Sequence[Mapping[str, Any]],
    options: ValidatorOptions,
) -> ValidationResult:
    """Validate local structural contract of transformed events."""
    result = ValidationResult(ok=True, proof_kind='local_structure_contract')
    events = list(events)
    result.stats['event_count'] = len(events)

    for err in events_are_json_serializable(events):
        result.add(ValidatorErrorCode.JSON_LINE, err)

    if not events:
        result.add(ValidatorErrorCode.STRUCTURE, 'empty')
        return result

    first = events[0]
    if first.get('type') != 'user':
        result.add(ValidatorErrorCode.STRUCTURE, 'first_event_not_user')
    if first.get('parentUuid') is not None:
        result.add(ValidatorErrorCode.PARENT, 'first_parent_not_null')

    uuids: set[str] = set()
    tool_use_ids: dict[str, str] = {}
    tool_result_ids: dict[str, str] = {}
    pending_tool_uses: set[str] = set()
    session_ids: set[str] = set()

    for idx, evt in enumerate(events):
        line_no = idx + 1
        if not isinstance(evt, dict):
            result.add(ValidatorErrorCode.STRUCTURE, f'line_{line_no}_not_object')
            continue

        etype = evt.get('type')
        if etype == 'summary':
            if options.forbid_summary:
                result.add(ValidatorErrorCode.SUMMARY, f'line_{line_no}')
            continue
        if etype not in ALLOWED_EVENT_TYPES:
            if options.unknown_event_mode == 'reject':
                result.add(ValidatorErrorCode.UNKNOWN_EVENT, f'line_{line_no}:{etype}')
            else:
                result.warnings.append(f'unknown_event:line_{line_no}:{etype}')
            continue

        uid = str(evt.get('uuid') or '')
        if not uid or not UUID_RE.match(uid):
            result.add(ValidatorErrorCode.UUID, f'line_{line_no}_bad_uuid')
        elif uid in uuids:
            result.add(ValidatorErrorCode.UUID, f'duplicate:{uid}')
        else:
            uuids.add(uid)

        sid = str(evt.get('sessionId') or '')
        if sid:
            session_ids.add(sid)

        if options.forbid_sidechain and evt.get('isSidechain') is True:
            result.add(ValidatorErrorCode.SIDECHAIN, uid or str(line_no))

        parent = evt.get('parentUuid')
        if idx == 0:
            if parent is not None:
                result.add(ValidatorErrorCode.PARENT, 'first_parent_not_null')
        else:
            if not parent or str(parent) not in uuids:
                result.add(ValidatorErrorCode.PARENT, f'line_{line_no}_bad_parent')

        message = evt.get('message') if isinstance(evt.get('message'), dict) else {}
        role = message.get('role')
        if role != etype:
            result.add(ValidatorErrorCode.STRUCTURE, f'line_{line_no}_role_mismatch')

        if etype == 'assistant':
            blocks = _content_blocks(message)
            seen_thinking_after_other = False
            for b in blocks:
                btype = b.get('type')
                if btype in {'thinking', 'redacted_thinking'}:
                    if options.thinking_policy == ThinkingPolicy.DROP:
                        result.add(ValidatorErrorCode.THINKING, 'thinking_present_under_drop')
                    if seen_thinking_after_other:
                        result.add(ValidatorErrorCode.THINKING, 'thinking_after_non_thinking')
                    thinking = str(b.get('thinking') or '').strip()
                    if btype == 'thinking' and not thinking:
                        result.add(ValidatorErrorCode.THINKING, 'empty_thinking')
                    if btype == 'redacted_thinking' and not b.get('data'):
                        result.add(ValidatorErrorCode.THINKING, 'empty_redacted_thinking')
                else:
                    seen_thinking_after_other = True
                if btype == 'tool_use':
                    tid = str(b.get('id') or '')
                    if not tid:
                        result.add(ValidatorErrorCode.TOOL_ID, 'empty_tool_use_id')
                    elif not TOOL_ID_RE.match(tid):
                        result.add(ValidatorErrorCode.TOOL_ID, f'bad_tool_use_id:{tid}')
                    elif tid in tool_use_ids:
                        result.add(ValidatorErrorCode.TOOL_ID, f'duplicate_tool_use:{tid}')
                    else:
                        tool_use_ids[tid] = uid
                        pending_tool_uses.add(tid)
                if btype == 'tool_result':
                    result.add(ValidatorErrorCode.TOOL_PAIR, 'tool_result_in_assistant')

        if etype == 'user':
            content = message.get('content')
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get('type') != 'tool_result':
                        continue
                    tid = str(b.get('tool_use_id') or '')
                    if not tid:
                        result.add(ValidatorErrorCode.TOOL_ID, 'empty_tool_result_id')
                    elif not TOOL_ID_RE.match(tid):
                        result.add(ValidatorErrorCode.TOOL_ID, f'bad_tool_result_id:{tid}')
                    elif tid in tool_result_ids:
                        result.add(ValidatorErrorCode.TOOL_PAIR, f'duplicate_tool_result:{tid}')
                    elif tid not in pending_tool_uses:
                        result.add(ValidatorErrorCode.TOOL_PAIR, f'tool_result_before_use:{tid}')
                    else:
                        tool_result_ids[tid] = uid
                        pending_tool_uses.discard(tid)

    for tid in pending_tool_uses:
        result.add(ValidatorErrorCode.TOOL_PAIR, f'orphan_tool_use:{tid}')
    for tid, _evt in tool_result_ids.items():
        if tid not in tool_use_ids:
            result.add(ValidatorErrorCode.TOOL_PAIR, f'orphan_tool_result:{tid}')

    if len(session_ids) != 1:
        result.add(ValidatorErrorCode.SESSION, f'count:{len(session_ids)}')
    elif options.session_id not in session_ids:
        result.add(ValidatorErrorCode.SESSION, 'mismatch')

    round_count = _count_real_rounds(events)
    result.stats['round_count'] = round_count
    if options.expected_round_count is not None and round_count != options.expected_round_count:
        result.add(
            ValidatorErrorCode.ROUND_COUNT,
            f'{round_count}!={options.expected_round_count}',
        )
    if options.max_round_count is not None and round_count > options.max_round_count:
        result.add(
            ValidatorErrorCode.ROUND_COUNT,
            f'{round_count}>{options.max_round_count}',
        )

    if options.max_output_bytes is not None:
        payload = '\n'.join(
            json.dumps(e, ensure_ascii=False, separators=(',', ':')) for e in events
        )
        nbytes = len(payload.encode('utf-8'))
        result.stats['output_bytes'] = nbytes
        if nbytes > options.max_output_bytes:
            result.add(ValidatorErrorCode.BUDGET, f'{nbytes}>{options.max_output_bytes}')

    if options.old_uuids:
        hits: list[str] = []
        for evt in events:
            _walk_uuid_tokens(
                evt,
                path='',
                old_uuids=set(options.old_uuids),
                allow_suffixes=options.uuid_path_allowlist,
                hits=hits,
            )
        for hit in hits:
            result.add(ValidatorErrorCode.OLD_UUID_RESIDUAL, hit)

    result.ok = len(result.errors) == 0
    result.stats.update({
        'tool_use_count': len(tool_use_ids),
        'tool_result_count': len(tool_result_ids),
        'proof_kind': result.proof_kind,
    })
    return result
