"""Continuity Native-Fork eligibility resolver.

Read-only. Answers one question: may this sealed generation job borrow the
live main-chat Claude session through an official ``fork_session`` child
instead of the isolated one-shot?

The continuity producer runs as its own cron process, so it cannot see the
frontend's in-memory ``ResidentSession`` (whose ``last_cache_refresh_*``
clock is process-local). Every fact below is therefore proven from durable,
cross-process evidence that already exists:

  * exact current parent — ``chat.window_identity.read_current_window_identity_conn``
    (the canonical current-window resolver) must name the same
    ``context_id``/``context_epoch`` as the candidate's snapshot, and
    ``chat.session_registry.get_context_claude_session`` must hold a ``READY``
    row for exactly that ``(context_id, resident_generation)``;
  * parent provider/model — the parent transcript JSONL, written by the
    provider itself: the boundary assistant event and the most recent
    assistant request (parsed with ``tools.cc_jsonl_usage._request_record``)
    must both report the job's frozen explicit model. A frozen ``default``
    identity has no model name to compare and is refused;
  * cache freshness — that most recent assistant request's timestamp must be
    younger than ``cc_resident.STALE_CACHE_MAX_AGE_SECONDS``, the same window
    the resident's own stale-cache guard uses;
  * exact boundary — the candidate's last completed turn maps to exactly one
    assistant event of that session/generation in
    ``chat_message_claude_events``.

Provider configuration (the settings page) is never used as live-model proof.
Anything not positively proven is a refusal; the caller then runs the
existing isolated one-shot with the job's own frozen model.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from continuity.contracts import ContinuityGenerationJob, SourceSnapshot
from continuity.sealing import CandidateBlock
from tools.cc_jsonl_usage import _request_record, session_jsonl_path

FLAG_KEY = 'CC_CONTINUITY_NATIVE_FORK_ENABLED'

MODE_NATIVE = 'native_fork'
MODE_ONESHOT = 'isolated_oneshot'

# Tolerated wall-clock skew between the provider's transcript timestamps and
# this process; a larger "future" timestamp is untrusted and refused.
_CLOCK_SKEW_SECONDS = 60.0

REASON_FLAG_OFF = 'flag_off'
REASON_AUTHORITY_UNFROZEN = 'authority_unfrozen'
REASON_UNSUPPORTED_PROVIDER = 'unsupported_provider'
REASON_MODEL_UNATTESTABLE = 'model_unattestable'
REASON_SOURCE_UNAVAILABLE = 'source_unavailable'
REASON_WINDOW_IDENTITY_MISSING = 'window_identity_missing'
REASON_NOT_CURRENT_WINDOW = 'not_current_window'
REASON_SCOPE_UNSUPPORTED = 'scope_unsupported'
REASON_SESSION_REGISTRY_MISSING = 'session_registry_missing'
REASON_SESSION_NOT_READY = 'session_not_ready'
REASON_PARENT_TRANSCRIPT_MISSING = 'parent_transcript_missing'
REASON_TRANSCRIPT_PATH_MISMATCH = 'transcript_path_mismatch'
REASON_MAPPING_MISSING = 'mapping_missing'
REASON_MAPPING_AMBIGUOUS = 'mapping_ambiguous'
REASON_SESSION_MISMATCH = 'session_mismatch'
REASON_BOUNDARY_NOT_IN_TRANSCRIPT = 'boundary_not_in_transcript'
REASON_PARENT_MODEL_MISMATCH = 'parent_model_mismatch'
REASON_PARENT_USAGE_MISSING = 'parent_usage_missing'
REASON_PARENT_COLD = 'parent_cold'
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
    parent_model: str = ''
    parent_last_use_age_seconds: Optional[float] = None
    scope_completed_turns: int = 0
    context_id: int = 0
    context_epoch: int = 0
    resident_generation: int = 0

    def observability(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'continuity_generation_mode': MODE_NATIVE if self.eligible else MODE_ONESHOT,
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


def _last_completed_turn_boundary(candidate: CandidateBlock) -> Optional[int]:
    """Assistant ``chat_messages.id`` of the last member, or ``None``.

    Only candidates built purely from ``turn:<user>:<assistant>`` members are
    supported; a Wake tail or malformed ref is refused.
    """
    refs = tuple(candidate.source_refs or ())
    if not refs:
        return None
    last: Optional[int] = None
    for ref in refs:
        parts = str(ref).split(':')
        if len(parts) != 3 or parts[0] != 'turn':
            return None
        try:
            last = int(parts[2])
        except (TypeError, ValueError):
            return None
    return last


def _assistant_mapping_rows(conn: sqlite3.Connection, message_id: int) -> list[dict[str, Any]]:
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


def _parse_timestamp(raw: Any) -> Optional[float]:
    text = str(raw or '').strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _transcript_evidence(path: Path, fork_uuid: str) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Return (boundary event model, most recent assistant request record)."""
    boundary_model: Optional[str] = None
    latest: Optional[dict[str, Any]] = None
    latest_ts: Optional[float] = None
    with path.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get('uuid') or '') == fork_uuid and row.get('type') == 'assistant':
                message = row.get('message')
                model = message.get('model') if isinstance(message, dict) else None
                boundary_model = str(model or '').strip() or None
            record = _request_record(row)
            if record is None:
                continue
            ts = _parse_timestamp(record.get('timestamp'))
            if ts is None:
                continue
            if latest_ts is None or ts >= latest_ts:
                latest, latest_ts = dict(record, _ts=ts), ts
    return boundary_model, latest


def resolve_continuity_native_fork(
    source_conn: Optional[sqlite3.Connection],
    *,
    job: ContinuityGenerationJob,
    candidate: CandidateBlock,
    snapshot: SourceSnapshot,
    cwd: str,
    claude_home: Optional[str] = None,
    now_wall: Optional[float] = None,
    require_flag: bool = True,
) -> ContinuityForkPlan:
    """Fail-closed eligibility for continuity native fork."""

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

        from chat.cc_model import cc_model_from_identity

        try:
            expected_model = cc_model_from_identity(job.frozen_model_identity)
        except ValueError:
            return _reject(REASON_MODEL_UNATTESTABLE)
        if not expected_model:
            return _reject(REASON_MODEL_UNATTESTABLE)

        if source_conn is None:
            return _reject(REASON_SOURCE_UNAVAILABLE)
        if snapshot.context_id is None or snapshot.context_epoch is None:
            return _reject(REASON_WINDOW_IDENTITY_MISSING)
        context_id = int(snapshot.context_id)
        context_epoch = int(snapshot.context_epoch)

        boundary_message_id = _last_completed_turn_boundary(candidate)
        if boundary_message_id is None:
            return _reject(REASON_SCOPE_UNSUPPORTED)

        from chat.window_identity import WindowIdentityUnavailable, read_current_window_identity_conn

        try:
            current = read_current_window_identity_conn(source_conn, chat_id=snapshot.chat_id)
        except WindowIdentityUnavailable:
            return _reject(REASON_NOT_CURRENT_WINDOW)
        if int(current['context_id']) != context_id or int(current['context_epoch']) != context_epoch:
            return _reject(REASON_NOT_CURRENT_WINDOW)
        resident_generation = int(current['resident_generation'])

        from chat.session_registry import get_context_claude_session

        registry = get_context_claude_session(context_id, resident_generation, conn=source_conn)
        if registry is None:
            return _reject(REASON_SESSION_REGISTRY_MISSING)
        if int(registry.get('context_epoch') or -1) != context_epoch:
            return _reject(REASON_NOT_CURRENT_WINDOW)
        if str(registry.get('scan_status') or '') != 'READY':
            return _reject(REASON_SESSION_NOT_READY)
        parent_sid = str(registry.get('claude_session_id') or '').strip()
        if not parent_sid:
            return _reject(REASON_SESSION_REGISTRY_MISSING)

        registered_path = Path(str(registry.get('transcript_path') or ''))
        derived_path = session_jsonl_path(cwd, parent_sid, claude_home=claude_home)
        if derived_path is None or registered_path != derived_path:
            # fork_session locates the parent by cwd; it must be the same file
            # whose hash we guard.
            return _reject(REASON_TRANSCRIPT_PATH_MISMATCH)
        try:
            if not registered_path.is_file() or registered_path.stat().st_size <= 0:
                return _reject(REASON_PARENT_TRANSCRIPT_MISSING)
        except OSError:
            return _reject(REASON_PARENT_TRANSCRIPT_MISSING)

        a_maps = _assistant_mapping_rows(source_conn, boundary_message_id)
        if not a_maps:
            return _reject(REASON_MAPPING_MISSING)
        for row in a_maps:
            if str(row.get('claude_session_id') or '') != parent_sid:
                return _reject(REASON_SESSION_MISMATCH)
            if (
                int(row.get('context_id') or -1) != context_id
                or int(row.get('context_epoch') or -1) != context_epoch
                or int(row.get('resident_generation') or -1) != resident_generation
            ):
                return _reject(REASON_SESSION_MISMATCH)
        top = a_maps[0]
        tied = [r for r in a_maps if r.get('jsonl_byte_offset') == top.get('jsonl_byte_offset')]
        if len({str(r.get('event_uuid')) for r in tied}) > 1:
            return _reject(REASON_MAPPING_AMBIGUOUS)
        fork_uuid = str(top.get('event_uuid') or '').strip()
        if not fork_uuid:
            return _reject(REASON_MAPPING_MISSING)

        boundary_model, latest = _transcript_evidence(registered_path, fork_uuid)
        if boundary_model is None:
            return _reject(REASON_BOUNDARY_NOT_IN_TRANSCRIPT)
        if latest is None:
            return _reject(REASON_PARENT_USAGE_MISSING)
        if boundary_model != expected_model or latest.get('model') != expected_model:
            return _reject(REASON_PARENT_MODEL_MISMATCH)

        from cc_resident import STALE_CACHE_MAX_AGE_SECONDS

        now = time.time() if now_wall is None else float(now_wall)
        age = now - float(latest['_ts'])
        if age < -_CLOCK_SKEW_SECONDS or age >= STALE_CACHE_MAX_AGE_SECONDS:
            return _reject(REASON_PARENT_COLD)

        return ContinuityForkPlan(
            eligible=True,
            reason='',
            generation_job_id=job.generation_job_id,
            candidate_id=candidate.candidate_id,
            parent_session_id=parent_sid,
            fork_event_uuid=fork_uuid,
            boundary_message_id=boundary_message_id,
            parent_transcript_path=str(registered_path),
            parent_model=expected_model,
            parent_last_use_age_seconds=max(0.0, age),
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
