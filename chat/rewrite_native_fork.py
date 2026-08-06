"""Opportunistic Claude native session fork for staged rewrite (R0).

Cache optimization only. Fail closed → caller keeps #204 cold bootstrap.

Does not advance durable history epoch. Child sessions are trial-only.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from tools.cc_jsonl_usage import session_jsonl_path

log = logging.getLogger('hayagarden.rewrite_native_fork')

FLAG_KEY = 'CC_REWRITE_NATIVE_FORK_ENABLED'

MODE_NATIVE = 'native_fork'
MODE_COLD = 'cold_fallback'

REASON_FLAG_OFF = 'flag_off'
REASON_UNSUPPORTED_PROVIDER = 'unsupported_provider'
REASON_MAPPING_MISSING = 'mapping_missing'
REASON_MAPPING_AMBIGUOUS = 'mapping_ambiguous'
REASON_SESSION_MISMATCH = 'session_mismatch'
REASON_EVENT_MISSING = 'event_missing'
REASON_PARENT_TRANSCRIPT_MISSING = 'parent_transcript_missing'
REASON_NO_SAFE_PRE_USER_BOUNDARY = 'no_safe_pre_user_boundary'
REASON_UNSUPPORTED_BOUNDARY = 'unsupported_boundary'
REASON_SDK_UNAVAILABLE = 'sdk_unavailable'
REASON_FORK_FAILED = 'fork_failed'
REASON_PARENT_MUTATED = 'parent_mutated'
REASON_CHILD_RESUME_FAILED = 'child_resume_failed'
REASON_CHILD_HEALTH_FAILED = 'child_health_failed'
REASON_RESOLVER_ERROR = 'resolver_error'
REASON_SOURCE_MISSING = 'source_missing'


@dataclass(frozen=True)
class NativeForkPlan:
    eligible: bool
    reason: str
    operation: str = ''
    parent_session_id: str = ''
    fork_event_uuid: str = ''
    source_message_id: int = 0
    rewrite_user_message_id: int = 0
    resend_content: str = ''
    parent_transcript_path: str = ''
    mapping_provenance: dict[str, Any] = field(default_factory=dict)

    def observability(self) -> dict[str, Any]:
        mode = MODE_NATIVE if self.eligible else MODE_COLD
        out = {
            'rewrite_cache_mode': mode,
            'rewrite_cache_fallback_reason': None if self.eligible else self.reason,
        }
        if self.parent_session_id:
            out['rewrite_cache_parent_session_hash'] = _redact_id(self.parent_session_id)
        if self.fork_event_uuid:
            out['rewrite_cache_fork_event_hash'] = _redact_id(self.fork_event_uuid)
        return out


@dataclass
class NativeForkExecution:
    ok: bool
    reason: str
    plan: NativeForkPlan
    child_session_id: str = ''
    parent_sha256_before: str = ''
    parent_sha256_after: str = ''
    observability: dict[str, Any] = field(default_factory=dict)


def _redact_id(value: str) -> str:
    raw = str(value or '').strip().encode('utf-8')
    if not raw:
        return ''
    return hashlib.sha256(raw).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def flag_enabled() -> bool:
    try:
        import config_store
        return bool(config_store.get_bool(FLAG_KEY, False))
    except Exception:
        return False


def import_fork_session() -> tuple[Optional[Callable[..., Any]], Optional[str]]:
    """Load official ``fork_session`` if present with expected signature."""
    try:
        from claude_agent_sdk import fork_session as _fork
    except Exception as exc:
        return None, 'sdk_import:%s' % type(exc).__name__
    try:
        sig = inspect.signature(_fork)
    except Exception as exc:
        return None, 'sdk_signature:%s' % type(exc).__name__
    params = sig.parameters
    required = ('session_id', 'directory', 'up_to_message_id')
    if any(name not in params for name in required):
        return None, 'sdk_signature_mismatch'
    return _fork, None


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def _rows_as_dicts(conn, sql: str, params: tuple) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        if isinstance(row, Mapping):
            out.append(dict(row))
        else:
            out.append({cols[i]: row[i] for i in range(len(cols))})
    return out


def _user_mapping_rows(conn, message_id: int) -> list[dict[str, Any]]:
    return _rows_as_dicts(
        conn,
        '''SELECT event_uuid, message_id, role, claude_session_id,
                  context_id, context_epoch, resident_generation, jsonl_byte_offset
           FROM chat_message_claude_events
           WHERE message_id=? AND role='user'
           ORDER BY jsonl_byte_offset ASC, event_uuid ASC''',
        (int(message_id),),
    )


def _assistant_boundary_rows(
    conn,
    *,
    before_message_id: int,
    claude_session_id: str,
) -> list[dict[str, Any]]:
    """Canonical previous-assistant candidates for fork (same session, id < user)."""
    return _rows_as_dicts(
        conn,
        '''SELECT event_uuid, message_id, role, claude_session_id,
                  context_id, context_epoch, resident_generation, jsonl_byte_offset
           FROM chat_message_claude_events
           WHERE role='assistant'
             AND message_id < ?
             AND claude_session_id=?
           ORDER BY message_id DESC, jsonl_byte_offset DESC, event_uuid ASC''',
        (int(before_message_id), str(claude_session_id)),
    )


def resolve_rewrite_native_fork(
    conn,
    staging: Mapping[str, Any],
    *,
    cwd: str,
    claude_home: Optional[str] = None,
    require_flag: bool = True,
) -> NativeForkPlan:
    """Fail-closed eligibility for opportunistic native fork."""
    op = str(staging.get('operation') or '').strip()
    source_message_id = int(staging.get('source_message_id') or 0)

    def _reject(reason: str, **extra: Any) -> NativeForkPlan:
        return NativeForkPlan(
            eligible=False,
            reason=reason,
            operation=op,
            source_message_id=source_message_id,
            mapping_provenance=dict(extra),
        )

    try:
        if require_flag and not flag_enabled():
            return _reject(REASON_FLAG_OFF)

        if source_message_id <= 0:
            return _reject(REASON_SOURCE_MISSING)

        src = conn.execute(
            'SELECT id, author, content FROM chat_messages WHERE id=?',
            (source_message_id,),
        ).fetchone()
        if not src:
            return _reject(REASON_SOURCE_MISSING)

        if op == 'regen':
            rewrite_user_id = staging.get('user_message_id')
            if rewrite_user_id is None:
                return _reject(REASON_SOURCE_MISSING, detail='regen_user_missing')
            rewrite_user_id = int(rewrite_user_id)
            user_row = conn.execute(
                'SELECT id, author, content FROM chat_messages WHERE id=?',
                (rewrite_user_id,),
            ).fetchone()
            if not user_row:
                return _reject(REASON_SOURCE_MISSING, detail='regen_user_row_missing')
            resend_content = str(user_row['content'] if isinstance(user_row, Mapping) else user_row[2] or '')
        elif op == 'edit':
            rewrite_user_id = source_message_id
            resend_content = str(staging.get('edited_content') or '').strip()
            if not resend_content:
                return _reject(REASON_UNSUPPORTED_BOUNDARY, detail='edited_content_empty')
        else:
            return _reject(REASON_UNSUPPORTED_BOUNDARY, detail='unknown_operation')

        if not _table_exists(conn, 'chat_message_claude_events'):
            return _reject(REASON_MAPPING_MISSING, detail='mapping_table_absent')

        user_maps = _user_mapping_rows(conn, rewrite_user_id)
        if not user_maps:
            return _reject(REASON_MAPPING_MISSING)
        if len(user_maps) != 1:
            return _reject(
                REASON_MAPPING_AMBIGUOUS,
                detail='user_mapping_cardinality',
                count=len(user_maps),
            )

        user_map = user_maps[0]
        parent_sid = str(user_map.get('claude_session_id') or '').strip()
        user_event = str(user_map.get('event_uuid') or '').strip()
        if not parent_sid or not user_event:
            return _reject(REASON_EVENT_MISSING, detail='user_mapping_incomplete')

        # Conflicting sessions among any mapped events on the rewrite user → refuse.
        sessions_on_user = {
            str(r['claude_session_id'])
            for r in conn.execute(
                'SELECT DISTINCT claude_session_id FROM chat_message_claude_events '
                'WHERE message_id=?',
                (rewrite_user_id,),
            ).fetchall()
            if r[0]
        }
        if len(sessions_on_user) != 1 or parent_sid not in sessions_on_user:
            return _reject(REASON_SESSION_MISMATCH, detail='user_multi_session')

        boundary_rows = _assistant_boundary_rows(
            conn,
            before_message_id=rewrite_user_id,
            claude_session_id=parent_sid,
        )
        if not boundary_rows:
            return _reject(REASON_NO_SAFE_PRE_USER_BOUNDARY)

        # Top message_id bucket only; refuse if that bucket spans sessions (defensive).
        top_mid = int(boundary_rows[0]['message_id'])
        top_bucket = [r for r in boundary_rows if int(r['message_id']) == top_mid]
        top_sessions = {str(r.get('claude_session_id') or '') for r in top_bucket}
        if top_sessions != {parent_sid}:
            return _reject(REASON_SESSION_MISMATCH, detail='boundary_session_mismatch')

        # Inclusive fork at the latest assistant event of the previous turn.
        fork_row = top_bucket[0]
        fork_uuid = str(fork_row.get('event_uuid') or '').strip()
        if not fork_uuid:
            return _reject(REASON_EVENT_MISSING, detail='boundary_uuid_missing')

        # Ambiguous: multiple distinct terminal assistant uuids at same offset identity.
        # Same message may have several assistant rows (tool rounds); we take the
        # highest jsonl_byte_offset (ORDER BY DESC). If offsets tie with different
        # uuids, refuse rather than guessing.
        tied = [
            r for r in top_bucket
            if r.get('jsonl_byte_offset') == fork_row.get('jsonl_byte_offset')
        ]
        if len({str(r.get('event_uuid')) for r in tied}) > 1:
            return _reject(REASON_MAPPING_AMBIGUOUS, detail='boundary_offset_tie')

        path = session_jsonl_path(cwd, parent_sid, claude_home=claude_home)
        if path is None or not path.is_file():
            return _reject(REASON_PARENT_TRANSCRIPT_MISSING)
        try:
            if path.stat().st_size <= 0:
                return _reject(REASON_PARENT_TRANSCRIPT_MISSING, detail='empty_transcript')
        except OSError:
            return _reject(REASON_PARENT_TRANSCRIPT_MISSING, detail='stat_failed')

        return NativeForkPlan(
            eligible=True,
            reason='',
            operation=op,
            parent_session_id=parent_sid,
            fork_event_uuid=fork_uuid,
            source_message_id=source_message_id,
            rewrite_user_message_id=rewrite_user_id,
            resend_content=resend_content,
            parent_transcript_path=str(path),
            mapping_provenance={
                'user_event_uuid_hash': _redact_id(user_event),
                'boundary_message_id': top_mid,
                'context_id': fork_row.get('context_id'),
                'resident_generation': fork_row.get('resident_generation'),
            },
        )
    except Exception as exc:
        log.exception('rewrite native fork resolve failed')
        return _reject(REASON_RESOLVER_ERROR, detail=type(exc).__name__)


def execute_native_fork(
    plan: NativeForkPlan,
    *,
    cwd: str,
    claude_home: Optional[str] = None,
    fork_session_fn: Optional[Callable[..., Any]] = None,
) -> NativeForkExecution:
    """Pure local fork via official SDK ``fork_session`` (no model call)."""
    obs = dict(plan.observability())
    if not plan.eligible:
        return NativeForkExecution(
            ok=False,
            reason=plan.reason or REASON_UNSUPPORTED_BOUNDARY,
            plan=plan,
            observability={**obs, 'rewrite_cache_mode': MODE_COLD},
        )

    parent_path = Path(plan.parent_transcript_path)
    try:
        before = _sha256_file(parent_path)
    except Exception:
        return NativeForkExecution(
            ok=False,
            reason=REASON_PARENT_TRANSCRIPT_MISSING,
            plan=plan,
            observability={
                **obs,
                'rewrite_cache_mode': MODE_COLD,
                'rewrite_cache_fallback_reason': REASON_PARENT_TRANSCRIPT_MISSING,
            },
        )

    fork_fn = fork_session_fn
    if fork_fn is None:
        fork_fn, err = import_fork_session()
        if fork_fn is None:
            return NativeForkExecution(
                ok=False,
                reason=REASON_SDK_UNAVAILABLE,
                plan=plan,
                parent_sha256_before=before,
                observability={
                    **obs,
                    'rewrite_cache_mode': MODE_COLD,
                    'rewrite_cache_fallback_reason': REASON_SDK_UNAVAILABLE,
                    'sdk_detail': err,
                },
            )

    try:
        result = fork_fn(
            plan.parent_session_id,
            directory=str(cwd),
            up_to_message_id=plan.fork_event_uuid,
            title=None,
        )
        child_sid = str(getattr(result, 'session_id', '') or '').strip()
        if not child_sid:
            raise RuntimeError('fork_session returned empty session_id')
    except Exception as exc:
        log.warning('rewrite native fork execute failed: %s', type(exc).__name__)
        after = before
        try:
            after = _sha256_file(parent_path)
        except Exception:
            pass
        reason = REASON_PARENT_MUTATED if after != before else REASON_FORK_FAILED
        return NativeForkExecution(
            ok=False,
            reason=reason,
            plan=plan,
            parent_sha256_before=before,
            parent_sha256_after=after,
            observability={
                **obs,
                'rewrite_cache_mode': MODE_COLD,
                'rewrite_cache_fallback_reason': reason,
            },
        )

    try:
        after = _sha256_file(parent_path)
    except Exception:
        after = ''
    if after != before:
        return NativeForkExecution(
            ok=False,
            reason=REASON_PARENT_MUTATED,
            plan=plan,
            child_session_id=child_sid,
            parent_sha256_before=before,
            parent_sha256_after=after,
            observability={
                **obs,
                'rewrite_cache_mode': MODE_COLD,
                'rewrite_cache_fallback_reason': REASON_PARENT_MUTATED,
            },
        )

    # Child transcript must exist for resume.
    home = claude_home
    child_path = session_jsonl_path(cwd, child_sid, claude_home=home)
    if child_path is None or not child_path.is_file():
        return NativeForkExecution(
            ok=False,
            reason=REASON_FORK_FAILED,
            plan=plan,
            child_session_id=child_sid,
            parent_sha256_before=before,
            parent_sha256_after=after,
            observability={
                **obs,
                'rewrite_cache_mode': MODE_COLD,
                'rewrite_cache_fallback_reason': REASON_FORK_FAILED,
                'detail': 'child_transcript_missing',
            },
        )

    return NativeForkExecution(
        ok=True,
        reason='',
        plan=plan,
        child_session_id=child_sid,
        parent_sha256_before=before,
        parent_sha256_after=after,
        observability={
            **obs,
            'rewrite_cache_mode': MODE_NATIVE,
            'rewrite_cache_fallback_reason': None,
            'rewrite_cache_child_session_hash': _redact_id(child_sid),
        },
    )


def try_prepare_native_trial_resident(
    *,
    staging: Mapping[str, Any],
    conn,
    cwd: str,
    system_text: str,
    env: Mapping[str, str],
    resident,
    claude_home: Optional[str] = None,
    fork_session_fn: Optional[Callable[..., Any]] = None,
    tool_profile: str = 'legacy',
) -> tuple[bool, dict[str, Any]]:
    """Resolve+fork+spawn_resumable on trial resident. Never raises to caller."""
    meta: dict[str, Any] = {
        'rewrite_cache_mode': MODE_COLD,
        'rewrite_cache_fallback_reason': REASON_FLAG_OFF,
    }
    try:
        if not flag_enabled():
            return False, meta

        plan = resolve_rewrite_native_fork(
            conn, staging, cwd=cwd, claude_home=claude_home, require_flag=True,
        )
        meta.update(plan.observability())
        if not plan.eligible:
            return False, meta

        execution = execute_native_fork(
            plan,
            cwd=cwd,
            claude_home=claude_home,
            fork_session_fn=fork_session_fn,
        )
        meta.update(execution.observability or {})
        if not execution.ok:
            return False, meta

        try:
            resident.spawn_resumable(
                system_text,
                dict(env),
                resume_session_id=execution.child_session_id,
                tool_profile=tool_profile,
                reason='rewrite_native_fork',
            )
            # Child JSONL identity guard during health window.
            child_path = session_jsonl_path(
                cwd, execution.child_session_id, claude_home=claude_home,
            )
            resident.wait_staged_health(
                jsonl_path=str(child_path) if child_path else None,
            )
        except Exception as exc:
            log.warning('rewrite native child resume failed: %s', type(exc).__name__)
            try:
                resident.invalidate_for_history_rewrite('rewrite_native_fork_resume_failed')
            except Exception:
                pass
            reason = (
                REASON_CHILD_HEALTH_FAILED
                if 'health' in str(exc).lower() or 'staged_' in str(exc)
                else REASON_CHILD_RESUME_FAILED
            )
            meta = {
                **meta,
                'rewrite_cache_mode': MODE_COLD,
                'rewrite_cache_fallback_reason': reason,
            }
            return False, meta

        meta = {
            **meta,
            'rewrite_cache_mode': MODE_NATIVE,
            'rewrite_cache_fallback_reason': None,
        }
        return True, meta
    except Exception as exc:
        log.exception('rewrite native trial prepare failed')
        try:
            resident.invalidate_for_history_rewrite('rewrite_native_fork_error')
        except Exception:
            pass
        return False, {
            'rewrite_cache_mode': MODE_COLD,
            'rewrite_cache_fallback_reason': REASON_RESOLVER_ERROR,
            'detail': type(exc).__name__,
        }
