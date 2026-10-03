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
REASON_MAPPING_GAP = 'mapping_gap'
REASON_MAPPING_AMBIGUOUS = 'mapping_ambiguous'
REASON_SESSION_MISMATCH = 'session_mismatch'
REASON_EVENT_MISSING = 'event_missing'
REASON_PARENT_TRANSCRIPT_MISSING = 'parent_transcript_missing'
REASON_NO_SAFE_PRE_USER_BOUNDARY = 'no_safe_pre_user_boundary'
REASON_UNSUPPORTED_BOUNDARY = 'unsupported_boundary'
REASON_PROFILE_UNSUPPORTED = 'parent_profile_unsupported'

# Soft Window is the only producer of chat_message_claude_events today.
_AI_AUTHORS = ('fyodor', 'assistant', 'claude')
_SOFT_WINDOW_TOOL_PROFILE = 'text_only'
_SOFT_WINDOW_STATIC_KIND = 'daily'
REASON_SDK_UNAVAILABLE = 'sdk_unavailable'
REASON_FORK_FAILED = 'fork_failed'
REASON_PARENT_MUTATED = 'parent_mutated'
REASON_CHILD_RESUME_FAILED = 'child_resume_failed'
REASON_CHILD_HEALTH_FAILED = 'child_health_failed'
REASON_RESOLVER_ERROR = 'resolver_error'
REASON_SOURCE_MISSING = 'source_missing'
REASON_CONTEXT_PLAN_REQUIRED = 'context_plan_required'
REASON_CONTEXT_PLAN_INVALID = 'context_plan_invalid'
REASON_CONTEXT_PLAN_RECEIPT_MISSING = 'context_plan_receipt_missing'
REASON_CONTEXT_PLAN_PREFIX_MISMATCH = 'context_plan_prefix_mismatch'
REASON_CONTEXT_PLAN_REPRESENTATION_MISMATCH = 'context_plan_representation_mismatch'
REASON_CONTEXT_PLAN_FIXED_SECTION_UNPROVEN = 'context_plan_fixed_section_unproven'
REASON_CONTEXT_PLAN_RUNTIME_IDENTITY_MISMATCH = 'context_plan_runtime_identity_mismatch'


@dataclass(frozen=True)
class NativeForkPlan:
    eligible: bool
    reason: str
    operation: str = ''
    parent_session_id: str = ''
    fork_event_uuid: str = ''
    source_message_id: int = 0
    rewrite_user_message_id: int = 0
    boundary_assistant_message_id: int = 0
    resend_content: str = ''
    parent_transcript_path: str = ''
    tool_profile: str = ''
    static_system_kind: str = ''
    mapping_provenance: dict[str, Any] = field(default_factory=dict)
    context_plan_proof: dict[str, Any] = field(default_factory=dict)

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
        if self.tool_profile:
            out['rewrite_cache_tool_profile'] = self.tool_profile
        if self.static_system_kind:
            out['rewrite_cache_static_system_kind'] = self.static_system_kind
        for key in (
            'context_plan_hash',
            'context_plan_source_hash',
            'context_plan_prefix_identity_hash',
        ):
            value = self.context_plan_proof.get(key)
            if value:
                out['rewrite_cache_' + key] = value
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


def _assistant_mapping_rows(conn, message_id: int) -> list[dict[str, Any]]:
    """Assistant mapping rows for one exact DB message id (no earlier fallback)."""
    return _rows_as_dicts(
        conn,
        '''SELECT event_uuid, message_id, role, claude_session_id,
                  context_id, context_epoch, resident_generation, jsonl_byte_offset
           FROM chat_message_claude_events
           WHERE message_id=? AND role='assistant'
           ORDER BY jsonl_byte_offset DESC, event_uuid ASC''',
        (int(message_id),),
    )


def _db_previous_assistant_id(conn, before_message_id: int) -> Optional[int]:
    """True A0 from active chat_messages: last AI row strictly before rewrite user."""
    row = conn.execute(
        '''SELECT id FROM chat_messages
           WHERE id < ?
             AND author IN ('fyodor', 'assistant', 'claude')
           ORDER BY id DESC
           LIMIT 1''',
        (int(before_message_id),),
    ).fetchone()
    if not row:
        return None
    if isinstance(row, Mapping):
        return int(row['id'])
    return int(row[0])


def _context_plan_member_identity(member: Any) -> dict[str, Any]:
    """Use the ContextPlan's existing member identity, not text comparison."""
    from continuity.context_plan import _member_identity
    return dict(_member_identity(member))


def _context_plan_representation_identity(representation: Any) -> tuple[Any, ...]:
    return (
        str(getattr(representation, 'representation_id', '') or ''),
        str(getattr(representation, 'kind', '') or ''),
        tuple(
            _context_plan_member_identity(member)
            for member in tuple(getattr(representation, 'source_members', ()) or ())
        ),
        str(getattr(representation, 'source_hash', '') or ''),
        int(getattr(representation, 'estimated_tokens', 0) or 0),
        str(getattr(representation, 'chunk_id', '') or ''),
        str(getattr(representation, 'candidate_id', '') or ''),
        str(getattr(representation, 'snapshot_id', '') or ''),
        tuple(getattr(representation, 'provenance', ()) or ()),
    )


def _source_ref_turn_ids(source_ref: Any) -> Optional[tuple[int, int]]:
    parts = str(source_ref or '').split(':')
    if len(parts) != 3 or parts[0] != 'turn':
        return None
    try:
        user_id, assistant_id = int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None
    if user_id <= 0 or assistant_id <= 0:
        return None
    return user_id, assistant_id


def _receipt_representation_identities(
    members: tuple[Any, ...],
    *,
    fork_boundary_message_id: int,
) -> tuple[tuple[Any, ...], ...]:
    """Group receipt members and retain only the prefix before the fork."""
    groups: list[tuple[str, str, list[Any]]] = []
    for member in members:
        kind = str(getattr(member, 'representation_kind', '') or '')
        if kind not in ('raw', 'chunk'):
            continue
        ref_ids = _source_ref_turn_ids(getattr(member, 'source_ref', ''))
        if ref_ids is None:
            continue
        if int(ref_ids[1]) > int(fork_boundary_message_id):
            continue
        rep_id = str(getattr(member, 'representation_id', '') or '')
        if not rep_id:
            continue
        if not groups or groups[-1][0] != rep_id or groups[-1][1] != kind:
            groups.append((rep_id, kind, []))
        groups[-1][2].append(member)
    return tuple(
        (
            rep_id,
            kind,
            tuple(_context_plan_member_identity(member) for member in group_members),
        )
        for rep_id, kind, group_members in groups
    )


def _context_plan_prefix_proof(
    conn,
    *,
    context_plan: Any,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    fork_boundary_message_id: int,
    rewrite_user_message_id: int,
    runtime_identity: Optional[Mapping[str, Any]],
) -> tuple[bool, str, dict[str, Any]]:
    """Prove that the existing parent already contains this plan prefix.

    The proof deliberately accepts only raw ContextPlan representations. A
    chunk body, accepted state, or open-loop carrier has no durable transcript
    identity at the fork boundary in the rewrite resolver, so resuming that
    child would be an unsafe semantic claim.
    """
    proof: dict[str, Any] = {}
    if context_plan is None:
        return False, REASON_CONTEXT_PLAN_REQUIRED, proof
    if not bool(getattr(context_plan, 'valid', False)):
        return False, REASON_CONTEXT_PLAN_INVALID, proof

    runtime = dict(runtime_identity or {})
    required_runtime = (
        'provider', 'model', 'effort', 'tool_profile', 'session_id',
        'static_system_sha256', 'context_id', 'context_epoch',
        'resident_generation',
    )
    if any(not str(runtime.get(key) or '').strip() for key in required_runtime):
        return False, REASON_CONTEXT_PLAN_RUNTIME_IDENTITY_MISMATCH, proof
    if (
        runtime.get('parent_model')
        and str(runtime.get('parent_model')) != str(runtime.get('model'))
    ) or (
        runtime.get('parent_effort')
        and str(runtime.get('parent_effort')) != str(runtime.get('effort'))
    ):
        return False, REASON_CONTEXT_PLAN_RUNTIME_IDENTITY_MISMATCH, proof
    if str(runtime.get('provider')) != 'claude_code':
        return False, REASON_CONTEXT_PLAN_RUNTIME_IDENTITY_MISMATCH, proof
    if str(runtime.get('tool_profile')) != _SOFT_WINDOW_TOOL_PROFILE:
        return False, REASON_CONTEXT_PLAN_RUNTIME_IDENTITY_MISMATCH, proof
    if (
        int(runtime.get('context_id')) != int(context_id)
        or int(runtime.get('context_epoch')) != int(context_epoch)
        or int(runtime.get('resident_generation')) != int(resident_generation)
    ):
        return False, REASON_CONTEXT_PLAN_RUNTIME_IDENTITY_MISMATCH, proof

    sections = tuple(getattr(context_plan, 'ordered_sections', ()) or ())
    current_request = tuple(
        section for section in sections
        if str(getattr(section, 'kind', '') or '') == 'current_request'
    )
    if len(current_request) != 1:
        return False, REASON_CONTEXT_PLAN_INVALID, proof
    representations = tuple(getattr(context_plan, 'representations', ()) or ())
    expected_groups: list[tuple[Any, ...]] = []
    for representation in representations:
        if str(getattr(representation, 'kind', '') or '') != 'raw':
            return False, REASON_CONTEXT_PLAN_REPRESENTATION_MISMATCH, proof
        members = tuple(getattr(representation, 'source_members', ()) or ())
        if not members:
            return False, REASON_CONTEXT_PLAN_REPRESENTATION_MISMATCH, proof
        for member in members:
            ids = _source_ref_turn_ids(getattr(member, 'source_ref', ''))
            if ids is None or int(ids[1]) > int(fork_boundary_message_id):
                return False, REASON_CONTEXT_PLAN_PREFIX_MISMATCH, proof
            if int(ids[0]) >= int(rewrite_user_message_id):
                return False, REASON_CONTEXT_PLAN_PREFIX_MISMATCH, proof
        expected_groups.append(_context_plan_representation_identity(representation))

    # Dynamic fixed sections are not represented by the durable message/event
    # mapping. Do not resume a child while pretending those carriers are in its
    # transcript. The invariant system is proven against the parent runtime.
    dynamic_kinds = {'accepted_state', 'accepted_open_loops'}
    if any(str(getattr(section, 'kind', '') or '') in dynamic_kinds for section in sections):
        return False, REASON_CONTEXT_PLAN_FIXED_SECTION_UNPROVEN, proof
    invariant_sections = tuple(
        section for section in sections
        if str(getattr(section, 'kind', '') or '') == 'invariant_system'
    )
    if len(invariant_sections) != 1:
        return False, REASON_CONTEXT_PLAN_FIXED_SECTION_UNPROVEN, proof
    if str(getattr(invariant_sections[0], 'content_hash', '') or '') != str(
        runtime.get('static_system_sha256') or ''
    ):
        return False, REASON_CONTEXT_PLAN_RUNTIME_IDENTITY_MISMATCH, proof

    try:
        from chat import context_receipt as receipt_store
        receipt = receipt_store.get_receipt(
            conn,
            context_id=int(context_id),
            context_epoch=int(context_epoch),
            resident_generation=int(resident_generation),
        )
        receipt_members = receipt_store.get_receipt_members(
            conn,
            context_id=int(context_id),
            context_epoch=int(context_epoch),
            resident_generation=int(resident_generation),
        )
    except Exception:
        return False, REASON_CONTEXT_PLAN_RECEIPT_MISSING, proof
    if receipt is None:
        return False, REASON_CONTEXT_PLAN_RECEIPT_MISSING, proof
    if (
        str(receipt.provider) != str(runtime.get('provider'))
        or str(receipt.model_identity) != str(runtime.get('model'))
        or str(receipt.session_id) != str(runtime.get('session_id'))
    ):
        return False, REASON_CONTEXT_PLAN_RUNTIME_IDENTITY_MISMATCH, proof
    if not receipt_members:
        return False, REASON_CONTEXT_PLAN_RECEIPT_MISSING, proof

    installed_groups = _receipt_representation_identities(
        tuple(receipt_members),
        fork_boundary_message_id=int(fork_boundary_message_id),
    )
    expected_simple = tuple(
        (
            identity[0],
            identity[1],
            identity[2],
        )
        for identity in expected_groups
    )
    if installed_groups != expected_simple:
        return False, REASON_CONTEXT_PLAN_PREFIX_MISMATCH, proof

    import json
    prefix_payload = {
        'representations': [
            {
                'representation_id': identity[0],
                'kind': identity[1],
                'source_members': [dict(member) for member in identity[2]],
                'source_hash': identity[3],
                'estimated_tokens': identity[4],
                'chunk_id': identity[5],
                'candidate_id': identity[6],
                'snapshot_id': identity[7],
                'provenance': [list(item) for item in identity[8]],
            }
            for identity in expected_groups
        ],
        'fork_boundary_message_id': int(fork_boundary_message_id),
        'rewrite_user_message_id': int(rewrite_user_message_id),
        'invariant_system': str(getattr(invariant_sections[0], 'content_hash')),
    }
    prefix_hash = hashlib.sha256(
        json.dumps(prefix_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    proof.update({
        'context_plan_hash': str(getattr(context_plan, 'plan_hash', '') or ''),
        'context_plan_source_hash': str(getattr(context_plan, 'source_hash', '') or ''),
        'context_plan_prefix_identity_hash': prefix_hash,
        'receipt_plan_hash': str(receipt.plan_hash),
    })
    return True, '', proof


def resolve_rewrite_native_fork(
    conn,
    staging: Mapping[str, Any],
    *,
    cwd: str,
    claude_home: Optional[str] = None,
    require_flag: bool = True,
    context_plan: Any = None,
    runtime_identity: Optional[Mapping[str, Any]] = None,
    require_context_plan: bool = False,
) -> NativeForkPlan:
    """Fail-closed eligibility for opportunistic native fork."""
    op = str(staging.get('operation') or '').strip()
    source_message_id = int(staging.get('source_message_id') or 0)

    def _reject(reason: str, **extra: Any) -> NativeForkPlan:
        proof = extra.pop('context_plan_proof', None) or {}
        return NativeForkPlan(
            eligible=False,
            reason=reason,
            operation=op,
            source_message_id=source_message_id,
            mapping_provenance=dict(extra),
            context_plan_proof=dict(proof),
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

        # Exact DB boundary first — never walk to an earlier mapped assistant.
        a0_id = _db_previous_assistant_id(conn, rewrite_user_id)
        if a0_id is None:
            return _reject(REASON_NO_SAFE_PRE_USER_BOUNDARY)

        a0_maps = _assistant_mapping_rows(conn, a0_id)
        if not a0_maps:
            return _reject(
                REASON_MAPPING_GAP,
                detail='a0_mapping_missing',
                boundary_assistant_message_id=a0_id,
            )

        a0_sessions = {str(r.get('claude_session_id') or '') for r in a0_maps}
        if a0_sessions != {parent_sid}:
            return _reject(
                REASON_SESSION_MISMATCH,
                detail='a0_session_mismatch',
                boundary_assistant_message_id=a0_id,
            )

        # Inclusive fork at latest assistant event of the exact A0 message.
        fork_row = a0_maps[0]
        fork_uuid = str(fork_row.get('event_uuid') or '').strip()
        if not fork_uuid:
            return _reject(REASON_EVENT_MISSING, detail='boundary_uuid_missing')

        # Same message may have several assistant rows (tool rounds); take the
        # highest jsonl_byte_offset. Offset ties with different uuids → refuse.
        tied = [
            r for r in a0_maps
            if r.get('jsonl_byte_offset') == fork_row.get('jsonl_byte_offset')
        ]
        if len({str(r.get('event_uuid')) for r in tied}) > 1:
            return _reject(REASON_MAPPING_AMBIGUOUS, detail='boundary_offset_tie')

        # Mapping rows are Soft Window-only today → preserve text_only + daily static.
        # Unknown/empty context identity is fail-closed (do not guess legacy MCP).
        context_id = fork_row.get('context_id')
        user_context_id = user_map.get('context_id')
        if context_id is None or user_context_id is None:
            return _reject(REASON_PROFILE_UNSUPPORTED, detail='missing_context_id')
        if int(context_id) != int(user_context_id):
            return _reject(REASON_SESSION_MISMATCH, detail='context_id_mismatch')
        if int(fork_row.get('context_epoch') or -1) != int(user_map.get('context_epoch') or -2):
            return _reject(REASON_SESSION_MISMATCH, detail='context_epoch_mismatch')
        if int(fork_row.get('resident_generation') or -1) != int(
            user_map.get('resident_generation') or -2
        ):
            return _reject(REASON_SESSION_MISMATCH, detail='resident_generation_mismatch')

        path = session_jsonl_path(cwd, parent_sid, claude_home=claude_home)
        if path is None or not path.is_file():
            return _reject(REASON_PARENT_TRANSCRIPT_MISSING)
        try:
            if path.stat().st_size <= 0:
                return _reject(REASON_PARENT_TRANSCRIPT_MISSING, detail='empty_transcript')
        except OSError:
            return _reject(REASON_PARENT_TRANSCRIPT_MISSING, detail='stat_failed')

        context_plan_proof: dict[str, Any] = {}
        if require_context_plan or context_plan is not None:
            safe, proof_reason, context_plan_proof = _context_plan_prefix_proof(
                conn,
                context_plan=context_plan,
                context_id=int(context_id),
                context_epoch=int(fork_row.get('context_epoch') or 0),
                resident_generation=int(fork_row.get('resident_generation') or 0),
                fork_boundary_message_id=int(a0_id),
                rewrite_user_message_id=int(rewrite_user_id),
                runtime_identity=runtime_identity,
            )
            if not safe:
                return _reject(
                    proof_reason,
                    context_plan_proof=context_plan_proof,
                )

        return NativeForkPlan(
            eligible=True,
            reason='',
            operation=op,
            parent_session_id=parent_sid,
            fork_event_uuid=fork_uuid,
            source_message_id=source_message_id,
            rewrite_user_message_id=rewrite_user_id,
            boundary_assistant_message_id=a0_id,
            resend_content=resend_content,
            parent_transcript_path=str(path),
            tool_profile=_SOFT_WINDOW_TOOL_PROFILE,
            static_system_kind=_SOFT_WINDOW_STATIC_KIND,
            mapping_provenance={
                'user_event_uuid_hash': _redact_id(user_event),
                'boundary_message_id': a0_id,
                'context_id': int(context_id),
                'context_epoch': int(fork_row.get('context_epoch') or 0),
                'resident_generation': int(fork_row.get('resident_generation') or 0),
            },
            context_plan_proof=context_plan_proof,
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


def _system_text_for_plan(plan: NativeForkPlan, fallback_system_text: str) -> str:
    """Parent Soft Window → daily text-only static; never invent a mixed profile."""
    if plan.static_system_kind == _SOFT_WINDOW_STATIC_KIND:
        from chat.system_builder import build_cc_daily_static_parts
        return build_cc_daily_static_parts()['full_system']
    return fallback_system_text


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
    tool_profile: Optional[str] = None,
    context_plan: Any = None,
    runtime_identity: Optional[Mapping[str, Any]] = None,
    require_context_plan: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """Resolve+fork+spawn_resumable on trial resident. Never raises to caller.

    ``tool_profile`` argument is ignored when the plan carries a parent profile;
    callers must not force legacy MCP onto Soft Window parents.
    """
    meta: dict[str, Any] = {
        'rewrite_cache_mode': MODE_COLD,
        'rewrite_cache_fallback_reason': REASON_FLAG_OFF,
    }
    try:
        if not flag_enabled():
            return False, meta

        plan = resolve_rewrite_native_fork(
            conn,
            staging,
            cwd=cwd,
            claude_home=claude_home,
            require_flag=True,
            context_plan=context_plan,
            runtime_identity=runtime_identity,
            require_context_plan=require_context_plan,
        )
        meta.update(plan.observability())
        if not plan.eligible:
            return False, meta

        child_profile = str(plan.tool_profile or '').strip()
        if not child_profile:
            return False, {
                **meta,
                'rewrite_cache_mode': MODE_COLD,
                'rewrite_cache_fallback_reason': REASON_PROFILE_UNSUPPORTED,
            }
        # Explicit override only allowed when it matches the resolved parent profile.
        if tool_profile is not None and str(tool_profile) != child_profile:
            return False, {
                **meta,
                'rewrite_cache_mode': MODE_COLD,
                'rewrite_cache_fallback_reason': REASON_PROFILE_UNSUPPORTED,
                'detail': 'tool_profile_override_mismatch',
            }

        execution = execute_native_fork(
            plan,
            cwd=cwd,
            claude_home=claude_home,
            fork_session_fn=fork_session_fn,
        )
        meta.update(execution.observability or {})
        if not execution.ok:
            return False, meta

        spawn_system = _system_text_for_plan(plan, system_text)
        try:
            resident.spawn_resumable(
                spawn_system,
                dict(env),
                resume_session_id=execution.child_session_id,
                tool_profile=child_profile,
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

