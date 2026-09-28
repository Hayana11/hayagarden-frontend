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
    ``chat_message_claude_events``; a trailing canonical Wake instead proves
    its provider-only transcript range with the existing reader and terminal
    round assertion, without creating a formal mapping.

Provider configuration (the settings page) is never used as live-model proof.
Anything not positively proven is a refusal; the caller then runs the
existing isolated one-shot with the job's own frozen model.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from continuity.contracts import ContinuityGenerationJob, SourceSnapshot
from continuity.sealing import CandidateBlock
from continuity.sources import _canonical_normal_wake, row_revision
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
REASON_WAKE_PROVENANCE_MISSING = 'wake_provenance_missing'
REASON_WAKE_PROVENANCE_MISMATCH = 'wake_provenance_mismatch'
REASON_WAKE_TRANSCRIPT_RANGE_INVALID = 'wake_transcript_range_invalid'
REASON_WAKE_TRANSCRIPT_SESSION_MISMATCH = 'wake_transcript_session_mismatch'
REASON_WAKE_TRANSCRIPT_ROUND_INVALID = 'wake_transcript_round_invalid'
REASON_WAKE_CONTEXT_MISMATCH = 'wake_context_mismatch'


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
    parent_cwd: str = ''
    parent_model: str = ''
    parent_last_use_age_seconds: Optional[float] = None
    scope_completed_turns: int = 0
    scope_wake_count: int = 0
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


def _turn_assistant_id(source_ref: Any) -> Optional[int]:
    parts = str(source_ref or '').split(':')
    if len(parts) != 3 or parts[0] != 'turn':
        return None
    try:
        user_id, assistant_id = int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None
    if user_id <= 0 or assistant_id <= 0:
        return None
    return assistant_id


def _wake_id(source_ref: Any) -> Optional[int]:
    parts = str(source_ref or '').split(':')
    if len(parts) != 2 or parts[0] != 'wake':
        return None
    try:
        wake_id = int(parts[1])
    except (TypeError, ValueError):
        return None
    return wake_id if wake_id > 0 else None


class _CandidateScopeRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _WakeTranscriptProvenance:
    wake_run_id: str
    chat_id: str
    context_id: int
    context_epoch: int
    resident_generation: int
    claude_session_id: str
    transcript_path: str
    start_offset: int
    end_offset: int
    fork_event_uuid: str


@dataclass(frozen=True)
class _CandidateScope:
    boundary_message_id: int
    turn_count: int
    wake_count: int
    trailing_wake: Optional[_WakeTranscriptProvenance] = None


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, 'keys') and key in row.keys():
            return row[key]
    except Exception:
        return default
    return default


def _strict_int(value: Any, *, minimum: int = 0) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _json_object(value: Any) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or ''))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _trailing_wake_provenance(
    source_conn: sqlite3.Connection,
    wake_row: Any,
    snapshot: SourceSnapshot,
) -> _WakeTranscriptProvenance:
    """Prove a trailing Wake's existing provider-only terminal boundary."""
    cache = _json_object(_row_value(wake_row, 'cache_info'))
    if cache is None:
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISSING)
    wake_run_id = str(cache.get('wake_run_id') or '').strip()
    skip = cache.get('transcript_skip')
    if (
        not wake_run_id
        or not isinstance(skip, dict)
        or skip.get('skipped_provider_round') is not True
    ):
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISSING)

    start_offset = _strict_int(skip.get('start_offset'), minimum=0)
    end_offset = _strict_int(skip.get('end_offset'), minimum=0)
    skip_context_id = _strict_int(skip.get('context_id'), minimum=1)
    skip_generation = _strict_int(skip.get('resident_generation'), minimum=1)
    if (
        start_offset is None
        or end_offset is None
        or end_offset <= start_offset
        or skip_context_id is None
        or skip_generation is None
    ):
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISMATCH)

    try:
        wake_rows = source_conn.execute(
            """SELECT wake_run_id, chat_id, context_id, context_epoch,
                      resident_generation
               FROM wake_log WHERE wake_run_id=? ORDER BY id ASC""",
            (wake_run_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISSING) from exc
    if len(wake_rows) != 1:
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISMATCH)
    wake_log = wake_rows[0]
    chat_id = str(_row_value(wake_log, 'chat_id') or '').strip()
    context_id = _strict_int(_row_value(wake_log, 'context_id'), minimum=1)
    context_epoch = _strict_int(_row_value(wake_log, 'context_epoch'), minimum=1)
    resident_generation = _strict_int(
        _row_value(wake_log, 'resident_generation'), minimum=1,
    )
    if (
        not chat_id
        or context_id is None
        or context_epoch is None
        or resident_generation is None
        or str(_row_value(wake_log, 'wake_run_id') or '').strip() != wake_run_id
        or chat_id != str(snapshot.chat_id)
        or context_id != int(snapshot.context_id)
        or context_epoch != int(snapshot.context_epoch)
        or skip_context_id != context_id
        or skip_generation != resident_generation
    ):
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISMATCH)

    try:
        registry_rows = source_conn.execute(
            """SELECT context_id, context_epoch, resident_generation,
                      chat_id, claude_session_id, transcript_path, scan_status
               FROM context_claude_sessions
               WHERE context_id=? AND resident_generation=?""",
            (context_id, resident_generation),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISSING) from exc
    if len(registry_rows) != 1:
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISMATCH)
    registry = registry_rows[0]
    claude_session_id = str(_row_value(registry, 'claude_session_id') or '').strip()
    transcript_path = str(_row_value(registry, 'transcript_path') or '').strip()
    if (
        _strict_int(_row_value(registry, 'context_id'), minimum=1) != context_id
        or _strict_int(_row_value(registry, 'context_epoch'), minimum=1) != context_epoch
        or _strict_int(_row_value(registry, 'resident_generation'), minimum=1)
        != resident_generation
        or str(_row_value(registry, 'chat_id') or '').strip() != chat_id
        or str(_row_value(registry, 'scan_status') or '') != 'READY'
        or not claude_session_id
        or not transcript_path
    ):
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISMATCH)

    try:
        from chat.claude_event_mapping import _assert_complete_terminal_round
        from chat.claude_transcript_reader import read_transcript_range

        graph = read_transcript_range(transcript_path, start_offset, end_offset)
    except Exception as exc:
        raise _CandidateScopeRejected(REASON_WAKE_TRANSCRIPT_RANGE_INVALID) from exc

    session_ids = {str(event.session_id or '').strip() for event in graph.events}
    if session_ids != {claude_session_id} or graph.session_id != claude_session_id:
        raise _CandidateScopeRejected(REASON_WAKE_TRANSCRIPT_SESSION_MISMATCH)
    if len(graph.candidate_rounds) != 1:
        raise _CandidateScopeRejected(REASON_WAKE_TRANSCRIPT_ROUND_INVALID)
    try:
        _assert_complete_terminal_round(graph, graph.candidate_rounds[0])
    except Exception as exc:
        raise _CandidateScopeRejected(REASON_WAKE_TRANSCRIPT_ROUND_INVALID) from exc

    return _WakeTranscriptProvenance(
        wake_run_id=wake_run_id,
        chat_id=chat_id,
        context_id=context_id,
        context_epoch=context_epoch,
        resident_generation=resident_generation,
        claude_session_id=claude_session_id,
        transcript_path=transcript_path,
        start_offset=start_offset,
        end_offset=end_offset,
        fork_event_uuid=graph.candidate_rounds[0].event_uuids[-1],
    )


def _candidate_scope(
    source_conn: sqlite3.Connection,
    candidate: CandidateBlock,
    snapshot: SourceSnapshot,
) -> Optional[_CandidateScope]:
    """Validate mixed source membership and return boundary/turn/wake counts.

    Canonical Wake truth stays in ``continuity.sources._canonical_normal_wake``;
    this resolver only binds that existing predicate to the frozen snapshot
    member revision.  No Wake generation is compared with the final boundary
    generation: interior Wake generations remain durable conversation material
    across a resident respawn; a trailing Wake is separately checked against
    the current session/context/generation before it becomes the fork boundary.
    """
    refs = tuple(candidate.source_refs or ())
    seqs = tuple(candidate.source_seqs or ())
    revisions = tuple(candidate.source_revisions or ())
    members = tuple(snapshot.members or ())
    if (
        candidate.snapshot_id != snapshot.snapshot_id
        or not refs
        or len(refs) != len(seqs)
        or len(refs) != len(revisions)
        or not members
    ):
        return None
    try:
        by_seq = {int(member.seq): member for member in members}
    except (TypeError, ValueError, AttributeError):
        return None
    if (
        len(by_seq) != len(members)
        or len(set(seqs)) != len(seqs)
        or tuple(seqs) != tuple(sorted(seqs))
    ):
        return None

    selected = []
    for seq, ref, revision in zip(seqs, refs, revisions):
        member = by_seq.get(int(seq))
        if (
            member is None
            or str(member.source_ref) != str(ref)
            or str(member.source_revision) != str(revision)
            or str(member.branch_id or snapshot.branch_id) != str(snapshot.branch_id)
        ):
            return None
        selected.append(member)

    last_kind = str(selected[-1].source_kind or '')
    turn_count = 0
    wake_count = 0
    for member, ref in zip(selected, refs):
        kind = str(member.source_kind or '')
        if kind == 'completed_turn':
            if _turn_assistant_id(ref) is None:
                return None
            turn_count += 1
            continue
        if kind != 'autonomous_event' or str(member.role or '') != 'assistant':
            return None
        wake_id = _wake_id(ref)
        if wake_id is None:
            return None
        try:
            row = source_conn.execute(
                'SELECT * FROM chat_messages WHERE id=?', (wake_id,),
            ).fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return None
        if row is None or not _canonical_normal_wake(row):
            return None
        try:
            revision = row_revision(row)
        except Exception:
            return None
        if revision != str(member.source_revision):
            return None
        wake_count += 1

    try:
        candidate_turn_count = int(candidate.completed_turn_count or 0)
    except (TypeError, ValueError):
        return None
    if candidate_turn_count != turn_count:
        return None

    if last_kind == 'completed_turn':
        boundary = _turn_assistant_id(refs[-1])
        if boundary is None:
            return None
        return _CandidateScope(
            boundary_message_id=boundary,
            turn_count=turn_count,
            wake_count=wake_count,
        )
    if last_kind != 'autonomous_event' or str(selected[-1].role or '') != 'assistant':
        return None
    trailing_wake_id = _wake_id(refs[-1])
    if trailing_wake_id is None:
        return None
    try:
        trailing_row = source_conn.execute(
            'SELECT * FROM chat_messages WHERE id=?', (trailing_wake_id,),
        ).fetchone()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISSING) from exc
    if trailing_row is None:
        raise _CandidateScopeRejected(REASON_WAKE_PROVENANCE_MISSING)
    trailing_wake = _trailing_wake_provenance(source_conn, trailing_row, snapshot)
    return _CandidateScope(
        boundary_message_id=0,
        turn_count=turn_count,
        wake_count=wake_count,
        trailing_wake=trailing_wake,
    )


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


def _attested_cwd(raw: Any) -> Optional[str]:
    """Normalize an absolute provider cwd; never resolve relative to caller cwd."""
    value = str(raw or '').strip()
    if not value or not os.path.isabs(value):
        return None
    return os.path.normpath(value)


def _transcript_evidence(
    path: Path,
    fork_uuid: str,
) -> tuple[Optional[str], Optional[dict[str, Any]], Optional[str]]:
    """Return boundary model, latest request, and one attested transcript cwd."""
    boundary_model: Optional[str] = None
    latest: Optional[dict[str, Any]] = None
    latest_ts: Optional[float] = None
    boundary_cwd: Optional[str] = None
    latest_cwd: Optional[str] = None
    cwd_values: set[str] = set()
    invalid_cwd = False
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
            raw_cwd = row.get('cwd')
            row_cwd = None
            if raw_cwd is not None:
                row_cwd = _attested_cwd(raw_cwd)
                if row_cwd is None:
                    invalid_cwd = True
                else:
                    cwd_values.add(row_cwd)
            if str(row.get('uuid') or '') == fork_uuid and row.get('type') == 'assistant':
                message = row.get('message')
                model = message.get('model') if isinstance(message, dict) else None
                boundary_model = str(model or '').strip() or None
                boundary_cwd = row_cwd
            record = _request_record(row)
            if record is None:
                continue
            ts = _parse_timestamp(record.get('timestamp'))
            if ts is None:
                continue
            if latest_ts is None or ts >= latest_ts:
                latest, latest_ts = dict(record, _ts=ts), ts
                latest_cwd = row_cwd
    transcript_cwd = None
    if (
        not invalid_cwd
        and len(cwd_values) == 1
        and (boundary_cwd is not None or latest_cwd is not None)
        and (boundary_cwd is None or boundary_cwd == next(iter(cwd_values)))
        and (latest_cwd is None or latest_cwd == next(iter(cwd_values)))
    ):
        transcript_cwd = boundary_cwd
        if transcript_cwd is None:
            transcript_cwd = latest_cwd
    return boundary_model, latest, transcript_cwd


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

        try:
            scope = _candidate_scope(source_conn, candidate, snapshot)
        except _CandidateScopeRejected as exc:
            return _reject(exc.reason)
        if scope is None:
            return _reject(REASON_SCOPE_UNSUPPORTED)
        boundary_message_id = scope.boundary_message_id
        turn_count = scope.turn_count
        wake_count = scope.wake_count

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
        try:
            if not registered_path.is_file() or registered_path.stat().st_size <= 0:
                return _reject(REASON_PARENT_TRANSCRIPT_MISSING)
        except OSError:
            return _reject(REASON_PARENT_TRANSCRIPT_MISSING)

        trailing_wake = scope.trailing_wake
        if trailing_wake is not None:
            if (
                trailing_wake.context_id != context_id
                or trailing_wake.context_epoch != context_epoch
                or trailing_wake.resident_generation != resident_generation
            ):
                return _reject(REASON_WAKE_CONTEXT_MISMATCH)
            if (
                trailing_wake.claude_session_id != parent_sid
                or Path(trailing_wake.transcript_path) != registered_path
            ):
                return _reject(REASON_WAKE_TRANSCRIPT_SESSION_MISMATCH)
            fork_uuid = str(trailing_wake.fork_event_uuid or '').strip()
            if not fork_uuid:
                return _reject(REASON_WAKE_TRANSCRIPT_ROUND_INVALID)
        else:
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

        boundary_model, latest, parent_cwd = _transcript_evidence(registered_path, fork_uuid)
        if parent_cwd is None:
            return _reject(REASON_TRANSCRIPT_PATH_MISMATCH)
        derived_path = session_jsonl_path(parent_cwd, parent_sid, claude_home=claude_home)
        if derived_path is None or registered_path != derived_path:
            # fork_session locates the parent by the attested cwd; it must be
            # the same file whose hash we guard.
            return _reject(REASON_TRANSCRIPT_PATH_MISMATCH)
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
            parent_cwd=parent_cwd,
            parent_model=expected_model,
            parent_last_use_age_seconds=max(0.0, age),
            scope_completed_turns=turn_count,
            scope_wake_count=wake_count,
            context_id=context_id,
            context_epoch=context_epoch,
            resident_generation=resident_generation,
        )
    except _CandidateScopeRejected as exc:
        return _reject(exc.reason)
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
