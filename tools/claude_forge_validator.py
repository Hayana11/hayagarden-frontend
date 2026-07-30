"""Structural validator for forged Claude Code JSONL transcripts (spike)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from tools.claude_forge_core import (
    TOOL_ID_RE,
    UUID_RE,
    _content_blocks,
    _has_legacy_injection,
    _message_text,
    load_jsonl,
    scan_unknown_uuid_strings,
    sha256_file,
    verify_safe_output_path,
)

FORBIDDEN_SIDECHAIN = 'FORGE_SIDECHAIN_UNSUPPORTED'
FORGE_UUID_REFERENCE_UNKNOWN = 'FORGE_UUID_REFERENCE_UNKNOWN'
FORGE_LEGACY_INJECTION = 'FORGE_LEGACY_INJECTION'
FORGE_STRUCTURE = 'FORGE_STRUCTURE'
FORGE_OUTPUT_PATH = 'FORGE_OUTPUT_PATH'
FORGE_SYMLINK = 'FORGE_SYMLINK'
FORGE_SHA_MISMATCH = 'FORGE_SHA_MISMATCH'
FORGE_THINKING_INVALID = 'FORGE_THINKING_INVALID'
FORGE_TOOL_PAIR = 'FORGE_TOOL_PAIR'
FORGE_TOOL_ORDER = 'FORGE_TOOL_ORDER'
ALLOWED_EVENT_TYPES = frozenset({'user', 'assistant', 'system'})


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def add(self, code: str, detail: str = '') -> None:
        msg = code if not detail else f'{code}:{detail}'
        self.errors.append(msg)
        self.ok = False


def validate_forged_transcript(
    events: Sequence[Mapping[str, Any]],
    *,
    session_id: str,
    output_path: Optional[Path] = None,
    expected_sha256: Optional[str] = None,
    allowed_output_root: Optional[Path] = None,
    old_uuids: Optional[set[str]] = None,
    forbid_legacy_injection: bool = True,
    forbid_sidechain: bool = True,
) -> ValidationResult:
    result = ValidationResult(ok=True)
    events = list(events)
    result.stats['event_count'] = len(events)

    if output_path is not None and allowed_output_root is not None:
        try:
            verify_safe_output_path(Path(output_path), allowed_output_root)
        except ValueError as exc:
            code = str(exc).split(':', 1)[0]
            result.add(code, str(exc))

    if not events:
        result.add(FORGE_STRUCTURE, 'empty_file')
        return result

    first = events[0]
    if first.get('type') != 'user':
        result.add(FORGE_STRUCTURE, 'first_event_not_user')
    if first.get('parentUuid') is not None:
        result.add(FORGE_STRUCTURE, 'first_parent_not_null')

    uuids: set[str] = set()
    tool_use_ids: dict[str, str] = {}
    tool_result_ids: dict[str, str] = {}
    session_ids: set[str] = set()
    pending_tool_uses: set[str] = set()

    for idx, evt in enumerate(events):
        line_no = idx + 1
        if not isinstance(evt, dict):
            result.add(FORGE_STRUCTURE, f'line_{line_no}_not_object')
            continue

        etype = evt.get('type')
        if etype not in ALLOWED_EVENT_TYPES:
            result.add(FORGE_STRUCTURE, f'line_{line_no}_bad_type:{etype}')

        uid = str(evt.get('uuid') or '')
        if not uid or not UUID_RE.match(uid):
            result.add(FORGE_STRUCTURE, f'line_{line_no}_bad_uuid')
        elif uid in uuids:
            result.add(FORGE_STRUCTURE, f'duplicate_uuid:{uid}')
        else:
            uuids.add(uid)

        sid = str(evt.get('sessionId') or '')
        if sid:
            session_ids.add(sid)
        if forbid_sidechain and evt.get('isSidechain') is True:
            result.add(FORBIDDEN_SIDECHAIN, uid or str(line_no))

        parent = evt.get('parentUuid')
        if idx == 0:
            if parent is not None:
                result.add(FORGE_STRUCTURE, 'first_parent_not_null')
        else:
            if not parent or str(parent) not in uuids:
                result.add(FORGE_STRUCTURE, f'line_{line_no}_bad_parent')

        message = evt.get('message') or {}
        role = message.get('role')
        if etype in {'user', 'assistant', 'system'} and role != etype:
            result.add(FORGE_STRUCTURE, f'line_{line_no}_role_mismatch')

        if etype == 'user' and forbid_legacy_injection:
            text = _message_text(message.get('content'))
            if _has_legacy_injection(text):
                result.add(FORGE_LEGACY_INJECTION, uid or str(line_no))

        if etype == 'assistant':
            blocks = _content_blocks(message)
            seen_thinking_after_other = False
            for b in blocks:
                btype = b.get('type')
                if btype in {'thinking', 'redacted_thinking'}:
                    if seen_thinking_after_other:
                        result.add(FORGE_THINKING_INVALID, 'thinking_after_non_thinking')
                    thinking = str(b.get('thinking') or '').strip()
                    if btype == 'thinking' and not thinking:
                        result.add(FORGE_THINKING_INVALID, 'empty_thinking')
                    if btype == 'redacted_thinking' and not b.get('data'):
                        result.add(FORGE_THINKING_INVALID, 'empty_redacted_thinking')
                else:
                    seen_thinking_after_other = True
                if btype == 'tool_use':
                    tid = str(b.get('id') or '')
                    if not tid or not TOOL_ID_RE.match(tid):
                        result.add(FORGE_TOOL_PAIR, f'bad_tool_use_id:{tid}')
                    elif tid in tool_use_ids:
                        result.add(FORGE_TOOL_PAIR, f'duplicate_tool_use:{tid}')
                    else:
                        tool_use_ids[tid] = uid
                        pending_tool_uses.add(tid)
                if btype == 'tool_result':
                    result.add(FORGE_TOOL_ORDER, 'tool_result_in_assistant')

        if etype == 'user':
            content = message.get('content')
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get('type') != 'tool_result':
                        continue
                    tid = str(b.get('tool_use_id') or '')
                    if not tid or not TOOL_ID_RE.match(tid):
                        result.add(FORGE_TOOL_PAIR, f'bad_tool_result_id:{tid}')
                    elif tid in tool_result_ids:
                        result.add(FORGE_TOOL_PAIR, f'duplicate_tool_result:{tid}')
                    elif tid not in pending_tool_uses:
                        result.add(FORGE_TOOL_ORDER, f'tool_result_before_use:{tid}')
                    else:
                        tool_result_ids[tid] = uid
                        pending_tool_uses.discard(tid)

    for tid in pending_tool_uses:
        result.add(FORGE_TOOL_PAIR, f'orphan_tool_use:{tid}')
    for tid in tool_result_ids:
        if tid not in tool_use_ids:
            result.add(FORGE_TOOL_PAIR, f'orphan_tool_result:{tid}')

    if len(session_ids) != 1:
        result.add(FORGE_STRUCTURE, f'session_id_count:{len(session_ids)}')
    elif session_id not in session_ids:
        result.add(FORGE_STRUCTURE, 'session_id_mismatch')

    if old_uuids:
        for evt in events:
            copied = json.loads(json.dumps(evt))
            result.errors.extend(scan_unknown_uuid_strings(copied, old_uuids))

    if output_path is not None:
        path = Path(output_path)
        if path.is_symlink():
            result.add(FORGE_SYMLINK, str(path))
        if expected_sha256 and path.is_file():
            actual = sha256_file(path)
            if actual != expected_sha256:
                result.add(FORGE_SHA_MISMATCH, actual)

    if output_path is not None and session_id:
        name = Path(output_path).name
        if name != f'{session_id}.jsonl':
            result.add(FORGE_STRUCTURE, 'filename_session_mismatch')

    result.ok = len(result.errors) == 0
    result.stats.update({
        'first_type': events[0].get('type') if events else None,
        'last_type': events[-1].get('type') if events else None,
        'tool_use_count': len(tool_use_ids),
        'tool_result_count': len(tool_result_ids),
    })
    return result


def validate_jsonl_file(path: Path, session_id: str, **kwargs: Any) -> ValidationResult:
    events = load_jsonl(path)
    return validate_forged_transcript(events, session_id=session_id, output_path=path, **kwargs)
