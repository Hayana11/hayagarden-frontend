"""Grounded source materialization for Continuity R3.

This module turns an already sealed candidate back into provider-readable
evidence.  It never discovers nearby messages, reads thinking, or supplies a
placeholder for a source it cannot verify.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from continuity.contracts import SourceMember, SourceSnapshot, candidate_source_revision
from continuity.coverage import source_hash
from continuity.sealing import CandidateBlock
from continuity.sources import (
    canonical_tool_outcome,
    derive_autonomous_events,
    derive_completed_turns,
)
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


class SourceMaterializationError(RuntimeError):
    """A frozen candidate no longer matches the durable source."""


class UnsupportedSourceError(SourceMaterializationError):
    """The candidate contains a source kind without a safe body resolver."""


@dataclass(frozen=True)
class MaterializedSource:
    """Provider-readable evidence plus the identity used for postcheck."""

    body: str
    source_token_estimate: int
    source_fingerprint: str
    source_refs: tuple[str, ...]


def _value(row: Any, key: str, default: Any = '') -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _json_value(raw: Any, default: Any) -> Any:
    if raw in (None, ''):
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _render_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return _canonical(value)
    if value is None:
        return ''
    return str(value)


_candidate_source_revision = candidate_source_revision


def _rows_by_id(rows: Iterable[Any]) -> dict[int, Any]:
    return {int(_value(row, 'id', 0) or 0): row for row in rows}


def _current_members(rows: tuple[Any, ...]) -> dict[str, SourceMember]:
    turns = derive_completed_turns(rows)
    events = derive_autonomous_events(rows)
    members: list[SourceMember] = []
    # build_source_members is deliberately imported lazily so this module's
    # public responsibility remains materialization, not segmentation.
    from continuity.sources import build_source_members

    members.extend(build_source_members(turns, events))
    return {member.source_ref: member for member in members}


def _turn_ids(source_ref: str) -> tuple[int, int]:
    parts = source_ref.split(':')
    if len(parts) != 3 or parts[0] != 'turn':
        raise SourceMaterializationError(f'invalid completed turn source ref: {source_ref}')
    try:
        return int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise SourceMaterializationError(f'invalid completed turn source ref: {source_ref}') from exc


def _wake_id(source_ref: str) -> int:
    parts = source_ref.split(':')
    if len(parts) != 2 or parts[0] != 'wake':
        raise SourceMaterializationError(f'invalid Wake source ref: {source_ref}')
    try:
        return int(parts[1])
    except ValueError as exc:
        raise SourceMaterializationError(f'invalid Wake source ref: {source_ref}') from exc


def _render_tool_outcomes(assistant_row: Any) -> list[str]:
    raw = _json_value(_value(assistant_row, 'tool_calls', ''), [])
    if not isinstance(raw, list):
        return []
    rendered: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        outcome = canonical_tool_outcome(item)
        rendered.append('\n'.join((
            f"NAME: {outcome['name'] or 'tool'}",
            f"ARGS: {_render_value(outcome['args'])}",
            f"RESULT: {_render_value(outcome['result'])}",
            f"SUCCESS: {str(outcome['success']).lower()}",
            f"ARTIFACT: {_render_value(outcome['artifact'])}",
            f"DIFF: {_render_value(outcome['diff'])}",
        )))
    return rendered


def _render_turn(source_ref: str, rows_by_id: Mapping[int, Any]) -> str:
    user_id, assistant_id = _turn_ids(source_ref)
    user_row = rows_by_id.get(user_id)
    assistant_row = rows_by_id.get(assistant_id)
    if user_row is None or assistant_row is None:
        raise SourceMaterializationError(f'raw rows missing for {source_ref}')
    body = [
        '[TURN]',
        'USER:',
        str(_value(user_row, 'content', '') or ''),
        '',
        'ASSISTANT:',
        str(_value(assistant_row, 'content', '') or ''),
    ]
    for outcome in _render_tool_outcomes(assistant_row):
        body.extend(('', 'TOOL OUTCOME:', outcome))
    return '\n'.join(body)


def _render_wake(source_ref: str, rows_by_id: Mapping[int, Any]) -> str:
    row = rows_by_id.get(_wake_id(source_ref))
    if row is None:
        raise SourceMaterializationError(f'raw row missing for {source_ref}')
    return '\n'.join((
        '[WAKE]',
        'ASSISTANT:',
        str(_value(row, 'content', '') or ''),
    ))


def materialize_source_members(
    members: Iterable[SourceMember],
    rows: Iterable[Any],
) -> MaterializedSource:
    """Materialize exact ContextPlan raw membership with canonical renderers."""
    selected = tuple(members)
    raw_rows = tuple(rows)
    rows_by_id = _rows_by_id(raw_rows)
    current_by_ref = _current_members(raw_rows)
    rendered: list[str] = []
    fingerprints: list[dict[str, Any]] = []
    seen_refs: set[str] = set()

    for member in selected:
        source_ref = str(member.source_ref)
        if source_ref in seen_refs:
            raise SourceMaterializationError(
                f'duplicate source member: {source_ref}'
            )
        seen_refs.add(source_ref)
        current_member = current_by_ref.get(source_ref)
        if current_member is None:
            raise SourceMaterializationError(
                f'source is no longer canonical: {source_ref}'
            )
        if (
            current_member.source_revision != member.source_revision
            or current_member.content_hash != member.content_hash
            or current_member.branch_id != member.branch_id
            or current_member.source_kind != member.source_kind
        ):
            raise SourceMaterializationError(
                f'source revision/content drift: {source_ref}'
            )
        if member.source_kind == 'completed_turn':
            rendered.append(_render_turn(source_ref, rows_by_id))
        elif member.source_kind == 'autonomous_event':
            rendered.append(_render_wake(source_ref, rows_by_id))
        else:
            raise UnsupportedSourceError(
                f'unsupported source kind: {member.source_kind}'
            )
        fingerprints.append({
            'seq': int(member.seq),
            'source_ref': source_ref,
            'source_revision': current_member.source_revision,
            'content_hash': current_member.content_hash,
            'branch_id': current_member.branch_id,
            'finality_status': 'completed',
        })

    body = '\n\n'.join(rendered)
    return MaterializedSource(
        body=body,
        source_token_estimate=int(estimate_tokens_heuristic_cjk1_ascii4_v1(body)),
        source_fingerprint=_sha256(fingerprints),
        source_refs=tuple(member.source_ref for member in selected),
    )


def materialize_candidate(
    snapshot: SourceSnapshot,
    candidate: CandidateBlock,
    rows: Iterable[Any],
) -> MaterializedSource:
    """Resolve exactly the candidate membership from the supplied raw rows."""
    if candidate.snapshot_id != snapshot.snapshot_id:
        raise SourceMaterializationError('candidate belongs to another snapshot')
    if source_hash(snapshot.members) != snapshot.source_hash:
        raise SourceMaterializationError('source snapshot hash mismatch')
    if (
        len(candidate.source_seqs) != len(candidate.source_refs)
        or len(candidate.source_seqs) != len(candidate.source_revisions)
    ):
        raise SourceMaterializationError('candidate membership length mismatch')

    raw_rows = tuple(rows)
    rows_by_id = _rows_by_id(raw_rows)
    members_by_ref = {member.source_ref: member for member in snapshot.members}
    current_by_ref = _current_members(raw_rows)
    rendered: list[str] = []
    fingerprints: list[dict[str, Any]] = []
    selected_members: list[SourceMember] = []

    for seq, source_ref, source_revision in zip(
        candidate.source_seqs, candidate.source_refs, candidate.source_revisions,
    ):
        stored_member = members_by_ref.get(source_ref)
        if stored_member is None or int(stored_member.seq) != int(seq):
            raise SourceMaterializationError(f'source member missing for {source_ref}')
        if stored_member.source_revision != source_revision:
            raise SourceMaterializationError(f'candidate revision mismatch for {source_ref}')
        if stored_member.source_kind == 'attachment_span':
            raise UnsupportedSourceError(f'attachment span cannot be materialized: {source_ref}')
        current_member = current_by_ref.get(source_ref)
        if current_member is None:
            raise SourceMaterializationError(f'source is no longer canonical: {source_ref}')
        if (
            current_member.source_revision != stored_member.source_revision
            or current_member.content_hash != stored_member.content_hash
            or current_member.branch_id != stored_member.branch_id
        ):
            raise SourceMaterializationError(f'source revision/content drift: {source_ref}')
        if current_member.source_kind != stored_member.source_kind:
            raise SourceMaterializationError(f'source kind drift: {source_ref}')
        selected_members.append(current_member)

        if stored_member.source_kind == 'completed_turn':
            rendered.append(_render_turn(source_ref, rows_by_id))
        elif stored_member.source_kind == 'autonomous_event':
            rendered.append(_render_wake(source_ref, rows_by_id))
        else:
            raise UnsupportedSourceError(f'unsupported source kind: {stored_member.source_kind}')
        fingerprints.append({
            'seq': int(seq),
            'source_ref': source_ref,
            'source_revision': current_member.source_revision,
            'content_hash': current_member.content_hash,
            'branch_id': current_member.branch_id,
            'finality_status': 'completed',
        })

    if _candidate_source_revision(selected_members) != candidate.source_revision:
        raise SourceMaterializationError('candidate source revision is stale')

    body = '\n\n'.join(rendered)
    return MaterializedSource(
        body=body,
        source_token_estimate=int(estimate_tokens_heuristic_cjk1_ascii4_v1(body)),
        source_fingerprint=_sha256(fingerprints),
        source_refs=tuple(candidate.source_refs),
    )

