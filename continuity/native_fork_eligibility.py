"""R1 — Continuity Native-Fork eligibility resolver.

Read-only. Answers exactly one question: can this already-sealed generation
job borrow the live main-chat Claude session (via an official ``fork_session``
child) instead of paying for a materialized-evidence isolated one-shot?

This module never mutates sealing, the Chunk schema, or settings authority.
It never calls a model and never forks a session. A ``ContinuityForkPlan`` is
pure evidence; ``continuity/native_fork_executor.py`` is the only module that
acts on it.

Deliberately narrow first version (frozen scope, tighten later, never
widen silently):
  * ``job.frozen_provider`` must already be ``'claude_code'``. Native fork is
    a Claude Code CLI session-transcript concept; there is no session or
    transcript to fork for ``api_relay``.
  * ``job.frozen_provider`` / ``job.frozen_model_identity`` must equal the
    *live* generation authority read right now, not the authority that was
    merely frozen onto this job at claim time. A different live model always
    falls back to the existing isolated one-shot untouched (R3).
  * The candidate's window identity must resolve to exactly one durably
    registered ``context_claude_sessions`` row for the same
    ``(context_id, context_epoch)`` scope, that row must be ``READY`` (never
    ``BLOCKED``), and its parent transcript file must exist on disk.
  * The candidate's last completed-turn source member must map to exactly one
    assistant event in ``chat_message_claude_events`` for that same session,
    context identity, and resident generation — the same fail-closed mapping
    shape already proven for staged rewrite in
    ``chat/rewrite_native_fork.py``. Any ambiguity, gap, or cross-session
    split refuses eligibility; it never guesses a boundary.
  * Only ordinary sealed candidates built purely from ``completed_turn``
    members are eligible. A candidate whose most recent member is an
    autonomous (Wake) event, or whose evidence mixes branches, is refused:
    those boundaries are not covered by the mapping table today.

Everything this module cannot positively prove is a refusal, not a guess.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Optional

from continuity.contracts import ContinuityGenerationJob, SourceSnapshot
from continuity.sealing import CandidateBlock
from tools.cc_jsonl_usage import session_jsonl_path

FLAG_KEY = 'CC_CONTINUITY_NATIVE_FORK_ENABLED'

MODE_NATIVE = 'native_fork'
MODE_ONESHOT = 'isolated_oneshot'

REASON_FLAG_OFF = 'flag_off'
REASON_AUTHORITY_UNFROZEN = 'authority_unfrozen'
REASON_UNSUPPORTED_PROVIDER = 'unsupported_provider'
REASON_MODEL_MISMATCH = 'model_mismatch'
REASON_WINDOW_IDENTITY_MISSING = 'window_identity_missing'
REASON_SCOPE_UNSUPPORTED = 'scope_unsupported'
REASON_SESSION_REGISTRY_MISSING = 'session_registry_missing'
REASON_SESSION_REGISTRY_AMBIGUOUS = 'session_registry_ambiguous'
REASON_SESSION_NOT_READY = 'session_not_ready'
REASON_PARENT_TRANSCRIPT_MISSING = 'parent_transcript_missing'
REASON_MAPPING_MISSING = 'mapping_missing'
REASON_MAPPING_AMBIGUOUS = 'mapping_ambiguous'
REASON_SESSION_MISMATCH = 'session_mismatch'
REASON_RESOLVER_ERROR = 'resolver_error'


@dataclass(frozen=True)
class ContinuityForkPlan:
    eligible: bool
    reason: str
    generation_job_id: str = ''
    candidate_id: str = ''
    parent_session_id: str = ''
    fork_event_uuid: str = ''
    boundary_message_id: int = 0
    parent_transcript_path: str = ''
    scope_completed_turns: int = 0
    context_id: int = 0
    context_epoch: int = 0
    resident_generation: int = 0

    def observability(self) -> dict[str, Any]:
        mode = MODE_NATIVE if self.eligible else MODE_ONESHOT
        out: dict[str, Any] = {
            'continuity_generation_mode': mode,
            'continuity_generation_fallback_reason': None if self.eligible else self.reason,
        }
        if self.parent_session_id:
            out['continuity_native_fork_parent_session_hash'] = _redact(self.parent_session_id)
        if self.fork_event_uuid:
            out['continuity_native_fork_boundary_event_hash'] = _redact(self.fork_event_uuid)
        return out


def _redact(value: str) -> str:
    raw = str(value or '').strip().encode('utf-8')
    if not raw:
        return ''
    return hashlib.sha256(raw).hexdigest()[:16]


def flag_enabled() -> bool:
    try:
        import config_store
        return bool(config_store.get_bool(FLAG_KEY, False))
    except Exception:
        return False


def _default_capture_authority() -> Any:
    from chat.provider_router import capture_generation_authority
    return capture_generation_authority()


def _last_completed_turn_boundary(candidate: CandidateBlock) -> Optional[int]:
    """Return the assistant ``chat_messages.id`` for the candidate's last member.

    Refuses (returns ``None``) unless every sealed member is an ordinary
    ``turn:<user_id>:<assistant_id>`` completed-turn ref; an autonomous event
    tail or any malformed ref makes the boundary unsupported.
    """
    refs = tuple(candidate.source_refs or ())
    if not refs:
        return None
    last_message_id: Optional[int] = None
    for ref in refs:
        parts = str(ref).split(':')
        if len(parts) != 3 or parts[0] != 'turn':
            return None
        try:
            last_message_id = int(parts[2])
        except (TypeError, ValueError):
            return None
    return last_message_id


def _registry_row(conn: sqlite3.Connection, *, context_id: int, context_epoch: int) -> Optional[dict[str, Any]]:
    rows = conn.execute(
        '''SELECT context_id, context_epoch, resident_generation, chat_id,
                  claude_session_id, transcript_path, scan_status, updated_at
           FROM context_claude_sessions
           WHERE context_id=? AND context_epoch=?
           ORDER BY resident_generation DESC''',
        (int(context_id), int(context_epoch)),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # More than one live resident generation has claimed this epoch;
        # do not guess which one is the current parent.
        distinct_sessions = {row['claude_session_id'] for row in rows}
        if len(distinct_sessions) > 1:
            return {'_ambiguous': True}
    row = rows[0]
    return dict(row)


def _assistant_mapping_rows(conn: sqlite3.Connection, message_id: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, 'chat_message_claude_events'):
        return []
    cur = conn.execute(
        '''SELECT event_uuid, message_id, role, claude_session_id,
                  context_id, context_epoch, resident_generation, jsonl_byte_offset
           FROM chat_message_claude_events
           WHERE message_id=? AND role='assistant'
           ORDER BY jsonl_byte_offset DESC, event_uuid ASC''',
        (int(message_id),),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def resolve_continuity_native_fork(
    conn: sqlite3.Connection,
    *,
    job: ContinuityGenerationJob,
    candidate: CandidateBlock,
    snapshot: SourceSnapshot,
    cwd: str,
    claude_home: Optional[str] = None,
    capture_authority: Optional[Callable[[], Any]] = None,
    require_flag: bool = True,
) -> ContinuityForkPlan:
    """Fail-closed eligibility for opportunistic continuity native fork."""

    def _reject(reason: str) -> ContinuityForkPlan:
        return ContinuityForkPlan(
            eligible=False,
            reason=reason,
            generation_job_id=job.generation_job_id,
            candidate_id=candidate.candidate_id,
        )

    try:
        if require_flag and not flag_enabled():
            return _reject(REASON_FLAG_OFF)

        if not job.frozen_provider or not job.frozen_model_identity:
            return _reject(REASON_AUTHORITY_UNFROZEN)
        if job.frozen_provider != 'claude_code':
            return _reject(REASON_UNSUPPORTED_PROVIDER)

        capture = capture_authority or _default_capture_authority
        live = capture()
        live_provider = str(getattr(live, 'provider', '') or '')
        live_model = str(getattr(live, 'model_identity', '') or '')
        if live_provider != job.frozen_provider or live_model != job.frozen_model_identity:
            return _reject(REASON_MODEL_MISMATCH)

        if snapshot.context_id is None or snapshot.context_epoch is None:
            return _reject(REASON_WINDOW_IDENTITY_MISSING)
        context_id = int(snapshot.context_id)
        context_epoch = int(snapshot.context_epoch)

        boundary_message_id = _last_completed_turn_boundary(candidate)
        if boundary_message_id is None:
            return _reject(REASON_SCOPE_UNSUPPORTED)

        registry = _registry_row(conn, context_id=context_id, context_epoch=context_epoch)
        if registry is None:
            return _reject(REASON_SESSION_REGISTRY_MISSING)
        if registry.get('_ambiguous'):
            return _reject(REASON_SESSION_REGISTRY_AMBIGUOUS)
        if str(registry.get('scan_status') or '') != 'READY':
            return _reject(REASON_SESSION_NOT_READY)
        parent_sid = str(registry.get('claude_session_id') or '').strip()
        resident_generation = int(registry.get('resident_generation') or 0)
        if not parent_sid:
            return _reject(REASON_SESSION_REGISTRY_MISSING)

        path = session_jsonl_path(cwd, parent_sid, claude_home=claude_home)
        if path is None or not path.is_file():
            return _reject(REASON_PARENT_TRANSCRIPT_MISSING)
        try:
            if path.stat().st_size <= 0:
                return _reject(REASON_PARENT_TRANSCRIPT_MISSING)
        except OSError:
            return _reject(REASON_PARENT_TRANSCRIPT_MISSING)

        a_maps = _assistant_mapping_rows(conn, boundary_message_id)
        if not a_maps:
            return _reject(REASON_MAPPING_MISSING)
        a_sessions = {str(r.get('claude_session_id') or '') for r in a_maps}
        if a_sessions != {parent_sid}:
            return _reject(REASON_SESSION_MISMATCH)
        for row in a_maps:
            if int(row.get('context_id') or -1) != context_id:
                return _reject(REASON_SESSION_MISMATCH)
            if int(row.get('context_epoch') or -1) != context_epoch:
                return _reject(REASON_SESSION_MISMATCH)
            if int(row.get('resident_generation') or -1) != resident_generation:
                return _reject(REASON_SESSION_MISMATCH)

        top = a_maps[0]
        tied = [r for r in a_maps if r.get('jsonl_byte_offset') == top.get('jsonl_byte_offset')]
        if len({str(r.get('event_uuid')) for r in tied}) > 1:
            return _reject(REASON_MAPPING_AMBIGUOUS)
        fork_uuid = str(top.get('event_uuid') or '').strip()
        if not fork_uuid:
            return _reject(REASON_MAPPING_MISSING)

        return ContinuityForkPlan(
            eligible=True,
            reason='',
            generation_job_id=job.generation_job_id,
            candidate_id=candidate.candidate_id,
            parent_session_id=parent_sid,
            fork_event_uuid=fork_uuid,
            boundary_message_id=boundary_message_id,
            parent_transcript_path=str(path),
            scope_completed_turns=int(candidate.completed_turn_count or 0),
            context_id=context_id,
            context_epoch=context_epoch,
            resident_generation=resident_generation,
        )
    except Exception:
        return _reject(REASON_RESOLVER_ERROR)


__all__ = [
    'FLAG_KEY',
    'MODE_NATIVE',
    'MODE_ONESHOT',
    'ContinuityForkPlan',
    'flag_enabled',
    'resolve_continuity_native_fork',
]
