"""Isolated Claude Code JSONL forge primitives for Manual Forge spike.

Pure functions only — no production DB, no real transcripts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from tools.cc_jsonl_usage import claude_project_slug

UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
TOOL_ID_RE = re.compile(r'^toolu_[A-Za-z0-9]+$')

STRIP_TOP_LEVEL_KEYS = frozenset({
    'requestId', 'request_id', 'promptId', 'prompt_id',
})

LEGACY_INJECTION_MARKERS = (
    '【昨日延续对话】',
    '以下是本聊天日内的正式对话记录：',
    '【新增正式对话】',
    '【渐变脑',
    'state_send_snapshot',
    'day_handoff',
    'current_day_history',
)

UUID_REFERENCE_PATHS = frozenset({
    'parentUuid',
    'leafUuid',
    'sourceToolUseID',
    'sourceToolAssistantUUID',
    'agentId',
})

TOOL_RESULT_ID_KEYS = frozenset({'tool_use_id'})


@dataclass
class ForgeOptions:
    new_session_id: str
    cwd: str
    version: str = '2.1.220-spike'
    keep_thinking: bool = True
    drop_thinking: bool = False
    exclude_sidechain: bool = True
    # event uuid -> canonical plain user text (CASE 7)
    user_canonical_by_event_uuid: dict[str, str] = field(default_factory=dict)
    primer_events: list[dict[str, Any]] = field(default_factory=list)
    primer_separator_meta: Optional[str] = None
    allowed_output_root: Optional[Path] = None


@dataclass
class ForgeResult:
    events: list[dict[str, Any]]
    uuid_map: dict[str, str]
    tool_id_map: dict[str, str]
    dropped_sidechain_uuids: list[str]
    stripped_injection_uuids: list[str]


def new_uuid() -> str:
    return str(uuid.uuid4())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding='utf-8') as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'invalid json at line {lineno}: {exc}') from exc
            if not isinstance(row, dict):
                raise ValueError(f'line {lineno} is not an object')
            events.append(row)
    return events


def collect_event_uuids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    found: set[str] = set()
    for evt in events:
        uid = str(evt.get('uuid') or '')
        if uid:
            found.add(uid)
    return found


def apply_uuid_map_deep(obj: Any, uuid_map: dict[str, str]) -> Any:
    """Recursively replace old UUID strings anywhere in the object tree."""
    if isinstance(obj, dict):
        return {k: apply_uuid_map_deep(v, uuid_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [apply_uuid_map_deep(v, uuid_map) for v in obj]
    if isinstance(obj, str) and obj in uuid_map:
        return uuid_map[obj]
    return obj


def verify_work_root(work_root: Path) -> Path:
    """Reject symlink work roots and symlink components before any resolve()."""
    if work_root.is_symlink():
        raise ValueError('FORGE_SYMLINK:work_root')
    current = work_root
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f'FORGE_SYMLINK:{current}')
        if current.parent == current:
            break
        current = current.parent
    return work_root.resolve()


def verify_safe_output_path(path: Path, allowed_root: Path) -> None:
    """Reject symlink escapes before writing forged JSONL."""
    root = verify_work_root(allowed_root)
    if path.is_symlink():
        raise ValueError(f'FORGE_SYMLINK:{path}')
    candidate = path if path.is_absolute() else (allowed_root / path)
    current = candidate.parent
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f'FORGE_SYMLINK:{current}')
        if current == root or current.parent == current:
            break
        current = current.parent
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f'FORGE_OUTPUT_PATH:{resolved}') from exc


def dump_jsonl(
    path: Path,
    events: Sequence[Mapping[str, Any]],
    *,
    allowed_output_root: Optional[Path] = None,
) -> str:
    lines = [json.dumps(evt, ensure_ascii=False, separators=(',', ':')) for evt in events]
    text = '\n'.join(lines) + ('\n' if lines else '')
    if allowed_output_root is not None:
        verify_safe_output_path(path, allowed_output_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = allowed_output_root / f'.forge-tmp-{new_uuid()}.jsonl'
        try:
            tmp.write_text(text, encoding='utf-8')
            os.replace(tmp, path)
        finally:
            if tmp.exists() and not path.exists():
                tmp.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return sha256_text(text)


def _is_conversational(evt: Mapping[str, Any]) -> bool:
    return evt.get('type') in {'user', 'assistant', 'system', 'summary'}


def _content_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get('content')
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text':
                parts.append(str(block.get('text') or ''))
        return ''.join(parts)
    return ''


def _has_legacy_injection(text: str) -> bool:
    return any(marker in text for marker in LEGACY_INJECTION_MARKERS)


def _strip_assistant_metadata(evt: dict[str, Any]) -> None:
    for key in list(evt.keys()):
        if key in STRIP_TOP_LEVEL_KEYS:
            evt.pop(key, None)
    message = evt.get('message')
    if isinstance(message, dict):
        message.pop('id', None)
        message.pop('model', None)
        message.pop('usage', None)


def _filter_thinking_blocks(blocks: list[dict[str, Any]], *, keep: bool, drop: bool) -> list[dict[str, Any]]:
    if drop:
        return [b for b in blocks if b.get('type') != 'thinking' and b.get('type') != 'redacted_thinking']
    if keep:
        return blocks
    return [b for b in blocks if b.get('type') != 'thinking' and b.get('type') != 'redacted_thinking']


def _remap_tool_blocks(
    blocks: list[dict[str, Any]],
    tool_id_map: dict[str, str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        b = copy.deepcopy(block)
        btype = b.get('type')
        if btype == 'tool_use':
            old_id = str(b.get('id') or '')
            if not old_id:
                continue
            new_id = tool_id_map.get(old_id)
            if new_id is None:
                new_id = f'toolu_{new_uuid().replace("-", "")[:20]}'
                tool_id_map[old_id] = new_id
            b['id'] = new_id
        elif btype == 'tool_result':
            old_id = str(b.get('tool_use_id') or '')
            if old_id in tool_id_map:
                b['tool_use_id'] = tool_id_map[old_id]
        out.append(b)
    return out


def _rebuild_user_event(
    evt: dict[str, Any],
    *,
    canonical_text: Optional[str],
    stripped: list[str],
) -> dict[str, Any]:
    out = copy.deepcopy(evt)
    message = out.setdefault('message', {})
    content = message.get('content')
    if canonical_text is not None:
        if _has_legacy_injection(_message_text(content)):
            stripped.append(str(out.get('uuid') or ''))
        message['role'] = 'user'
        message['content'] = canonical_text
        return out
    if isinstance(content, list):
        message['content'] = _remap_tool_blocks(content, {})
    return out


def collect_sidechain_roots(events: Sequence[Mapping[str, Any]]) -> set[str]:
    roots: set[str] = set()
    for evt in events:
        if evt.get('isSidechain') is True:
            uid = str(evt.get('uuid') or '')
            if uid:
                roots.add(uid)
    return roots


def expand_sidechain_closure(
    events: Sequence[Mapping[str, Any]],
    *,
    exclude_sidechain: bool,
) -> set[str]:
    if not exclude_sidechain:
        return set()
    by_uuid = {str(e.get('uuid') or ''): e for e in events if e.get('uuid')}
    excluded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for evt in events:
            uid = str(evt.get('uuid') or '')
            if not uid or uid in excluded:
                continue
            if evt.get('isSidechain') is True:
                excluded.add(uid)
                changed = True
                continue
            parent = evt.get('parentUuid')
            if parent and str(parent) in excluded:
                excluded.add(uid)
                changed = True
    # also exclude children of sidechain roots via parent chain
    for evt in events:
        uid = str(evt.get('uuid') or '')
        parent = evt.get('parentUuid')
        if parent and str(parent) in excluded and uid:
            if uid not in excluded:
                excluded.add(uid)
                changed = True
    return excluded


def forge_transcript(
    source_events: Sequence[Mapping[str, Any]],
    opts: ForgeOptions,
) -> ForgeResult:
    if opts.drop_thinking and opts.keep_thinking:
        raise ValueError('drop_thinking and keep_thinking are mutually exclusive')

    excluded = expand_sidechain_closure(source_events, exclude_sidechain=opts.exclude_sidechain)
    kept: list[dict[str, Any]] = []
    for evt in source_events:
        uid = str(evt.get('uuid') or '')
        if uid and uid in excluded:
            continue
        if evt.get('type') == 'summary':
            continue
        if evt.get('type') in {'result', 'file-history-snapshot'}:
            continue
        kept.append(copy.deepcopy(dict(evt)))

    uuid_map: dict[str, str] = {}
    tool_id_map: dict[str, str] = {}
    stripped: list[str] = []

    for evt in kept:
        old = str(evt.get('uuid') or '')
        if not old:
            old = new_uuid()
            evt['uuid'] = old
        uuid_map[old] = new_uuid()

    forged: list[dict[str, Any]] = []
    for evt in kept:
        old_uid = str(evt.get('uuid') or '')
        new_evt: dict[str, Any] = {}
        new_evt['type'] = evt.get('type')
        new_evt['uuid'] = uuid_map[old_uid]
        parent = evt.get('parentUuid')
        if parent is None:
            new_evt['parentUuid'] = None
        else:
            mapped = uuid_map.get(str(parent))
            new_evt['parentUuid'] = mapped if mapped else None
        new_evt['timestamp'] = evt.get('timestamp')
        new_evt['sessionId'] = opts.new_session_id
        new_evt['cwd'] = opts.cwd
        new_evt['version'] = opts.version
        if evt.get('isSidechain') is True:
            new_evt['isSidechain'] = True

        if new_evt['type'] == 'user':
            canonical = opts.user_canonical_by_event_uuid.get(old_uid)
            message = copy.deepcopy(evt.get('message') or {})
            content = message.get('content')
            if canonical is not None:
                if _has_legacy_injection(_message_text(content)):
                    stripped.append(old_uid)
                message = {'role': 'user', 'content': canonical}
            elif isinstance(content, list):
                message['content'] = _remap_tool_blocks(content, tool_id_map)
            new_evt['message'] = message
            for key in ('sourceToolUseID', 'toolUseResult'):
                if key in evt:
                    val = evt[key]
                    if key == 'sourceToolUseID' and isinstance(val, str) and val in tool_id_map:
                        new_evt[key] = tool_id_map[val]
                    else:
                        new_evt[key] = copy.deepcopy(val)
        elif new_evt['type'] == 'assistant':
            message = copy.deepcopy(evt.get('message') or {})
            blocks = _content_blocks(message)
            blocks = _filter_thinking_blocks(blocks, keep=opts.keep_thinking, drop=opts.drop_thinking)
            blocks = _remap_tool_blocks(blocks, tool_id_map)
            message['role'] = 'assistant'
            message['content'] = blocks
            new_evt['message'] = message
            _strip_assistant_metadata(new_evt)
        else:
            for key, val in evt.items():
                if key in {'type', 'uuid', 'parentUuid', 'timestamp', 'sessionId', 'cwd', 'message'}:
                    continue
                new_evt[key] = copy.deepcopy(val)

        forged.append(new_evt)

    # primer injection as synthetic prefix events
    if opts.primer_events:
        primer_forge = forge_transcript(
            opts.primer_events,
            ForgeOptions(
                new_session_id=opts.new_session_id,
                cwd=opts.cwd,
                version=opts.version,
                keep_thinking=opts.keep_thinking,
                drop_thinking=opts.drop_thinking,
                exclude_sidechain=opts.exclude_sidechain,
            ),
        )
        tool_id_map.update(primer_forge.tool_id_map)
        primer_chain = primer_forge.events
        if opts.primer_separator_meta and primer_chain and forged:
            sep_uuid = new_uuid()
            primer_chain = primer_chain + [{
                'type': 'system',
                'uuid': sep_uuid,
                'parentUuid': primer_chain[-1]['uuid'],
                'timestamp': forged[0].get('timestamp'),
                'sessionId': opts.new_session_id,
                'cwd': opts.cwd,
                'version': opts.version,
                'message': {'role': 'system', 'content': opts.primer_separator_meta},
            }]
            forged[0]['parentUuid'] = sep_uuid
        forged = primer_chain + forged

    # rebuild linear parent chain for main kept events if needed
    if forged:
        forged[0]['parentUuid'] = None
        for idx in range(1, len(forged)):
            forged[idx]['parentUuid'] = forged[idx - 1]['uuid']

    forged = [apply_uuid_map_deep(evt, uuid_map) for evt in forged]

    return ForgeResult(
        events=forged,
        uuid_map=uuid_map,
        tool_id_map=tool_id_map,
        dropped_sidechain_uuids=sorted(excluded),
        stripped_injection_uuids=stripped,
    )


def remap_uuid_references(obj: Any, uuid_map: dict[str, str], *, path: str = '$') -> list[str]:
    """Return error codes for unknown old UUID references."""
    errors: list[str] = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            child_path = f'{path}.{key}'
            if key in UUID_REFERENCE_PATHS or key in TOOL_RESULT_ID_KEYS:
                if isinstance(val, str) and UUID_RE.match(val):
                    if val in uuid_map:
                        obj[key] = uuid_map[val]
                    elif val not in set(uuid_map.values()):
                        errors.append(f'FORGE_UUID_REFERENCE_UNKNOWN:{child_path}')
            else:
                errors.extend(remap_uuid_references(val, uuid_map, path=child_path))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            errors.extend(remap_uuid_references(item, uuid_map, path=f'{path}[{idx}]'))
    elif isinstance(obj, str) and UUID_RE.match(obj):
        if obj in uuid_map:
            pass  # parent handles assignment in dict branch
        elif obj not in set(uuid_map.values()):
            # only flag if looks like old reference embedded in free text — skip
            pass
    return errors


def scan_unknown_uuid_strings(obj: Any, old_uuids: set[str], *, path: str = '$') -> list[str]:
    errors: list[str] = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            child = f'{path}.{key}'
            errors.extend(scan_unknown_uuid_strings(val, old_uuids, path=child))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            errors.extend(scan_unknown_uuid_strings(item, old_uuids, path=f'{path}[{idx}]'))
    elif isinstance(obj, str):
        if obj in old_uuids:
            errors.append(f'FORGE_UUID_REFERENCE_UNKNOWN:{path}')
        elif UUID_RE.match(obj) and obj in old_uuids:
            errors.append(f'FORGE_UUID_REFERENCE_UNKNOWN:{path}')
    return errors


def session_jsonl_path_for_cwd(cwd: str, session_id: str, *, claude_home: Optional[Path] = None) -> Path:
    home = claude_home or Path.home() / '.claude'
    return home / 'projects' / claude_project_slug(cwd) / f'{session_id}.jsonl'


def build_minimal_text_session(
    *,
    session_id: str,
    cwd: str,
    user_texts: Sequence[str],
    assistant_texts: Sequence[str],
) -> list[dict[str, Any]]:
    if len(user_texts) != len(assistant_texts):
        raise ValueError('user_texts and assistant_texts length mismatch')
    events: list[dict[str, Any]] = []
    parent: Optional[str] = None
    ts = '2026-07-29T12:00:00.000Z'
    for idx, (u, a) in enumerate(zip(user_texts, assistant_texts)):
        u_id = new_uuid()
        events.append({
            'type': 'user',
            'uuid': u_id,
            'parentUuid': parent,
            'timestamp': ts,
            'sessionId': session_id,
            'cwd': cwd,
            'version': '2.1.220-spike',
            'message': {'role': 'user', 'content': u},
        })
        a_id = new_uuid()
        events.append({
            'type': 'assistant',
            'uuid': a_id,
            'parentUuid': u_id,
            'timestamp': ts,
            'sessionId': session_id,
            'cwd': cwd,
            'version': '2.1.220-spike',
            'message': {
                'role': 'assistant',
                'content': [{'type': 'text', 'text': a}],
            },
        })
        parent = a_id
    return events
