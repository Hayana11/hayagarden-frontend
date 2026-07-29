"""Structural validator for forged Claude Code JSONL transcripts (spike)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from tools.claude_forge_core import (
    LEGACY_INJECTION_MARKERS,
    UUID_RE,
    _content_blocks,
    _has_legacy_injection,
    _message_text,
    load_jsonl,
    scan_unknown_uuid_strings,
    sha256_file,
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

    for idx, evt in enumerate(events):
        line_no = idx + 1
        if not isinstance(evt, dict):
            result.add(FORGE_STRUCTURE, f'line_{line_no}_not_object')
            continue
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

        etype = evt.get('type')
        message = evt.get('message') or {}
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
                    sig = str(b.get('signature') or '').strip()
                    data = b.get('data')
                    if btype == 'thinking':
                        if not thinking:
                            result.add(FORGE_THINKING_INVALID, 'empty_thinking')
                        if sig == '' and thinking:
                            # unsigned thinking allowed in some builds; warn only
                            result.warnings.append('unsigned_thinking_block')
                    if btype == 'redacted_thinking':
                        if not data:
                            result.add(FORGE_THINKING_INVALID, 'empty_redacted_thinking')
                else:
                    seen_thinking_after_other = True
                if btype == 'tool_use':
                    tid = str(b.get('id') or '')
                    if not tid:
                        result.add(FORGE_TOOL_PAIR, 'empty_tool_use_id')
                    elif tid in tool_use_ids:
                        result.add(FORGE_TOOL_PAIR, f'duplicate_tool_use:{tid}')
                    else:
                        tool_use_ids[tid] = uid
                if btype == 'tool_result':
                    tid = str(b.get('tool_use_id') or '')
                    if not tid:
                        result.add(FORGE_TOOL_PAIR, 'empty_tool_result_id')
                    else:
                        tool_result_ids[tid] = uid

        if etype == 'user':
            content = message.get('content')
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get('type') == 'tool_result':
                        tid = str(b.get('tool_use_id') or '')
                        if not tid:
                            result.add(FORGE_TOOL_PAIR, 'empty_tool_result_id')
                        else:
                            tool_result_ids[tid] = uid

    for tid, src in tool_use_ids.items():
        if tid not in tool_result_ids:
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
        if allowed_output_root is not None:
            try:
                path.resolve().relative_to(allowed_output_root.resolve())
            except ValueError:
                result.add(FORGE_OUTPUT_PATH, str(path))
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
