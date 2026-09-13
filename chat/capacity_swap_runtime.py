"""Capacity Swap Runtime (P-CONTEXT-WINDOW Step 8-B).

Thin orchestration: capacity-only pre-stdin interception, SYSTEM_SUFFIX_V1
boundary (never in forged JSONL), same-context generation handoff, and
exactly-once CURRENT user send.

Does not rewrite Step 8-A selection, Manual Forge, or cold fallback semantics.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from chat.capacity_swap import (
    CAPACITY_SWAP_REASONS,
    AnchorStatus,
    CapacitySwapCandidate,
    CapacitySwapPrepareResult,
    CapacitySwapStatus,
    prepare_capacity_swap_candidate,
)
from chat.claude_transcript_model import (
    EventRole,
    SidechainPolicy,
    SummaryPolicy,
    ThinkingPolicy,
    UnknownEventPolicy,
)
from chat.claude_transcript_transform import (
    SelectionPolicy,
    TransformError,
    TransformRequest,
    estimate_serialized_token_count,
    serialize_events,
    sha256_text,
    transform_transcript,
)
from chat.claude_transcript_validator import ValidatorOptions, validate_transcript_events
from chat.claude_transcript_reader import read_transcript_range
from chat.cold_bootstrap_budget import (
    cold_prompt_target,
    cold_safety_margin,
    estimate_text_tokens,
)
from chat.context_window_forge import atomic_write_jsonl_fsync
from chat.claude_event_mapping import (
    ERROR_MAPPING_LAG,
    attempt_mapping_catchup_through,
    latest_complete_assistant_watermark,
    registry_mapping_lags_watermark,
)
from chat.session_registry import (
    SCAN_STATUS_BLOCKED,
    derive_transcript_path,
    get_context_claude_session,
    register_context_claude_session,
)

logger = logging.getLogger(__name__)

CAPACITY_SWAP_REGISTRY_SOURCE = 'capacity_swap'

# Frozen Boundary representation (exact Spike 8-B0 string — do not rephrase).
CAPACITY_BOUNDARY_REPRESENTATION = 'SYSTEM_SUFFIX_V1'
CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1 = (
    '\n\n[容量边界]\n'
    '当前 Claude session 未包含本窗口更早的部分对话。'
    '不要假装精确记得缺失原话；'
    '用户引用缺失内容而无法确认时，应自然请求补充。'
)

# Same-context last-good (process-local; no new DB schema).
# Updated only after full provider result + assistant persist + JSONL growth + cursor commit.
_SAME_CONTEXT_LAST_GOOD: dict[tuple[int, int], dict[str, Any]] = {}


class CapacitySwapRuntimeError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = 'capacity_swap_runtime_error'):
        super().__init__(message)
        self.error_code = error_code


def clear_same_context_last_good_for_tests() -> None:
    _SAME_CONTEXT_LAST_GOOD.clear()


def note_same_context_last_good(
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    claude_session_id: str,
    source: str,
    transcript_end_offset: Optional[int] = None,
) -> None:
    """Record last-good only after a fully committed successful turn."""
    sid = str(claude_session_id or '').strip()
    if int(context_id) <= 0 or int(context_epoch) <= 0 or int(resident_generation) <= 0:
        return
    if not sid:
        return
    _SAME_CONTEXT_LAST_GOOD[(int(context_id), int(context_epoch))] = {
        'context_id': int(context_id),
        'context_epoch': int(context_epoch),
        'resident_generation': int(resident_generation),
        'claude_session_id': sid,
        'source': str(source or '').strip() or 'daily_runtime',
        'transcript_end_offset': (
            int(transcript_end_offset) if transcript_end_offset is not None else None
        ),
    }


def get_same_context_last_good(
    context_id: int,
    context_epoch: int,
) -> Optional[dict[str, Any]]:
    return _SAME_CONTEXT_LAST_GOOD.get((int(context_id), int(context_epoch)))


def try_restore_same_context_last_good(
    *,
    plan: Any,
    live_resident: Any,
    current_user_stdin_flushed: bool = False,
) -> dict[str, Any]:
    """Middle fallback after Capacity Swap fail, before cold respawn.

    Never resends CURRENT user. If stdin already flushed → fail closed.
    If last-good is the still-alive source generation → report preserved.
    Does not claim a sendable path when capacity reason remains (cold next).
    """
    forbid_resend_after_stdin_flush(
        current_user_sent=bool(current_user_stdin_flushed),
        action='same_context_last_good_resend',
    )
    lg = get_same_context_last_good(int(plan.context_id), int(plan.context_epoch))
    live_sid = str(getattr(live_resident, 'session_id', None) or '').strip()
    alive = False
    poll = getattr(getattr(live_resident, '_proc', None), 'poll', None)
    if callable(poll):
        try:
            alive = poll() is None
        except Exception:
            alive = False
    elif bool(getattr(live_resident, '_alive', False)):
        alive = True
    elif live_sid:
        # Test fakes often omit _proc; treat bound session as preservable.
        alive = True

    if lg is None:
        # No committed last-good yet: preserving the pre-swap live resident counts
        # as same-context last-good integrity for the fallback chain.
        return {
            'ok': False,
            'error_code': 'last_good_unrecorded',
            'preserved_live': bool(alive and live_sid),
            'live_session_id': live_sid or None,
        }

    if int(lg['resident_generation']) != int(plan.resident_generation):
        return {
            'ok': False,
            'error_code': 'last_good_generation_mismatch',
            'last_good': dict(lg),
            'preserved_live': bool(alive and live_sid == str(lg.get('claude_session_id') or '')),
        }

    if live_sid and live_sid == str(lg.get('claude_session_id') or '') and alive:
        return {
            'ok': False,  # still cannot clear capacity alone → cold next
            'error_code': 'last_good_preserved_pre_cold',
            'last_good': dict(lg),
            'preserved_live': True,
        }
    return {
        'ok': False,
        'error_code': 'last_good_not_restorable',
        'last_good': dict(lg),
        'preserved_live': False,
    }


@dataclass
class CapacitySwapRuntimeResult:
    ok: bool
    error_code: Optional[str] = None
    candidate: Optional[CapacitySwapCandidate] = None
    source_context_id: int = 0
    source_context_epoch: int = 0
    source_resident_generation: int = 0
    target_resident_generation: int = 0
    candidate_session_id: Optional[str] = None
    jsonl_path: Optional[str] = None
    jsonl_sha256: Optional[str] = None
    effective_system: str = ''
    warnings: list[str] = field(default_factory=list)
    current_user_in_candidate: bool = False


def is_capacity_swap_reason(reason: Optional[str]) -> bool:
    """Strict allowlist — never treat generic respawn as Capacity Swap."""
    return str(reason or '').strip() in CAPACITY_SWAP_REASONS


def with_capacity_boundary_suffix(system_text: str) -> str:
    """Append SYSTEM_SUFFIX_V1 once (idempotent)."""
    base = str(system_text or '')
    if CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1 in base:
        return base
    return base + CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1


def registry_uses_capacity_swap(registry: Optional[Mapping[str, Any]]) -> bool:
    if not registry:
        return False
    return str(registry.get('source') or '').strip() == CAPACITY_SWAP_REGISTRY_SOURCE


def effective_static_system_for_registry(
    static_system: str,
    registry: Optional[Mapping[str, Any]],
) -> str:
    """Restore identical effective_system for capacity_swap generations."""
    if registry_uses_capacity_swap(registry):
        return with_capacity_boundary_suffix(static_system)
    return str(static_system or '')


def resolve_finalize_registry_source(
    existing_registry: Optional[Mapping[str, Any]],
    *,
    default: str = 'daily_runtime',
) -> str:
    """Preserve authoritative Registry source on finalize (no identity overwrite)."""
    if existing_registry is not None:
        src = str(existing_registry.get('source') or '').strip()
        if src:
            return src
    return str(default or 'daily_runtime')


def compute_retained_transcript_token_budget(
    *,
    static_system: str,
    pending_user_text: str = '',
    dynamic_state_text: str = '',
) -> int:
    """Dynamic retained-transcript budget using existing estimators (no new tokenizer)."""
    target = int(cold_prompt_target())
    margin = int(cold_safety_margin())
    reserved = (
        estimate_text_tokens(static_system)
        + estimate_text_tokens(CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1)
        + estimate_text_tokens(dynamic_state_text)
        + estimate_text_tokens(pending_user_text)
        + margin
    )
    # Anchor gets a separate budget in Step 8-A; keep a conservative slice for it.
    anchor_reserve = max(256, margin // 4)
    budget = target - reserved - anchor_reserve
    return max(512, int(budget))


def compute_anchor_token_budget() -> int:
    return max(512, int(cold_safety_margin()) // 2)


def assert_current_user_excluded(
    *,
    candidate: CapacitySwapCandidate,
    current_user_message_id: int,
    current_user_content: str,
) -> None:
    mid = int(current_user_message_id)
    if mid > 0 and mid in set(candidate.selected_message_ids):
        raise CapacitySwapRuntimeError(
            'current user appears in candidate history',
            error_code='current_user_in_candidate',
        )
    body = str(current_user_content or '').strip()
    if body and body in (candidate.serialized_jsonl or ''):
        # Allow short common phrases only when message id already excluded;
        # still fail closed if the exact pending body is present as a user line.
        for line in (candidate.serialized_jsonl or '').splitlines():
            if not line.strip():
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get('type') != 'user':
                continue
            content = (evt.get('message') or {}).get('content')
            if content == body:
                raise CapacitySwapRuntimeError(
                    'current user content found in forged history',
                    error_code='current_user_in_candidate',
                )


def forbid_resend_after_stdin_flush(
    *,
    current_user_sent: bool,
    action: str = 'fallback_resend',
) -> None:
    """Hard gate: after stdin flush, CURRENT user must never be resent."""
    if current_user_sent:
        raise CapacitySwapRuntimeError(
            f'refuse {action}: current user already stdin-flushed',
            error_code='current_user_already_sent',
        )


def _snapshot_prefix_sha256(path: str, end_offset: int) -> str:
    import hashlib

    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        digest = hashlib.sha256()
        remaining = int(end_offset)
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                raise CapacitySwapRuntimeError(
                    'source transcript truncated',
                    error_code='source_transcript_truncated',
                )
            digest.update(chunk)
            remaining -= len(chunk)
        return digest.hexdigest()
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def _load_formal_messages_excluding_current(
    conn: Any,
    *,
    context_id: int,
    context_epoch: int,
    current_user_message_id: int,
    cursor_before: Optional[int],
) -> list[dict[str, Any]]:
    from chat.context_window import _collect_context_formal_messages

    rows = _collect_context_formal_messages(
        conn, context_id=int(context_id), context_epoch=int(context_epoch),
    )
    out: list[dict[str, Any]] = []
    cur_uid = int(current_user_message_id)
    limit = int(cursor_before) if cursor_before is not None else None
    for row in rows:
        mid = int(row['id'])
        if mid == cur_uid:
            continue
        if limit is not None and mid > limit:
            continue
        item = {
            'id': mid,
            'author': str(row['author'] or ''),
            'content': str(row['content'] or ''),
            'image_url': '',
            'file_url': '',
            'file_name': '',
            'attachments': '',
        }
        if hasattr(row, 'keys') and 'image_url' in row.keys():
            item['image_url'] = str(row['image_url'] or '')
        if hasattr(row, 'keys') and 'file_url' in row.keys():
            item['file_url'] = str(row['file_url'] or '')
        if hasattr(row, 'keys') and 'file_name' in row.keys():
            item['file_name'] = str(row['file_name'] or '')
        if hasattr(row, 'keys') and 'attachments' in row.keys():
            item['attachments'] = row['attachments']
        out.append(item)
    return out


def _load_mapping_for_messages(
    conn: Any,
    message_ids: Sequence[int],
) -> tuple[dict[str, Any], dict[int, str], dict[str, int]]:
    from chat.context_window_preview import (
        _canonical_user_payload_from_db,
        _mapping_rows_for_messages_conn,
    )

    rows = _mapping_rows_for_messages_conn(conn, list(message_ids))
    mid_to_event: dict[int, str] = {}
    event_to_mid: dict[str, int] = {}
    user_uuids: list[str] = []
    for row in rows:
        uid = str(row.get('event_uuid') or '').strip()
        mid = int(row.get('message_id') or 0)
        role = str(row.get('role') or '')
        if not uid or mid <= 0:
            continue
        event_to_mid[uid] = mid
        if role == 'user':
            mid_to_event[mid] = uid
            user_uuids.append(uid)

    canonical: dict[str, Any] = {}
    if user_uuids:
        placeholders = ','.join('?' for _ in user_uuids)
        cols = {
            str(r[1])
            for r in conn.execute('PRAGMA table_info(chat_messages)').fetchall()
        }
        image_col = 'm.image_url' if 'image_url' in cols else "'' AS image_url"
        file_url_col = 'm.file_url' if 'file_url' in cols else "'' AS file_url"
        file_name_col = 'm.file_name' if 'file_name' in cols else "'' AS file_name"
        attachments_col = 'm.attachments' if 'attachments' in cols else "'' AS attachments"
        db_rows = conn.execute(
            f'''SELECT e.event_uuid AS event_uuid, m.content AS content, {image_col},
                       {file_url_col}, {file_name_col}, {attachments_col}
                FROM chat_message_claude_events e
                JOIN chat_messages m ON m.id = e.message_id
                WHERE e.role = 'user' AND e.event_uuid IN ({placeholders})''',
            tuple(user_uuids),
        ).fetchall()
        for row in db_rows:
            canonical[str(row['event_uuid'])] = _canonical_user_payload_from_db(
                content=str(row['content'] or ''),
                image_url=str(row['image_url'] or ''),
                file_url=str(row['file_url'] or ''),
                file_name=str(row['file_name'] or ''),
                attachments=row['attachments'],
            )
    return canonical, mid_to_event, event_to_mid



def _context_plan_raw_members(context_plan: Any) -> list[tuple[Any, int, int]]:
    """Return exact completed-turn members selected by one ContextPlan."""
    raw_members: list[tuple[Any, int, int]] = []
    for representation in tuple(getattr(context_plan, 'representations', ()) or ()):
        if str(getattr(representation, 'kind', '') or '') != 'raw':
            continue
        for member in tuple(getattr(representation, 'source_members', ()) or ()):
            source_kind = str(getattr(member, 'source_kind', '') or '')
            source_ref = str(getattr(member, 'source_ref', '') or '')
            parts = source_ref.split(':')
            if source_kind != 'completed_turn' or len(parts) != 3 or parts[0] != 'turn':
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw source is not a completed turn',
                    error_code='context_plan_capacity_raw_source_invalid',
                )
            try:
                user_id = int(parts[1])
                assistant_id = int(parts[2])
            except (TypeError, ValueError) as exc:
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw source reference is invalid',
                    error_code='context_plan_capacity_raw_source_invalid',
                ) from exc
            if user_id <= 0 or assistant_id <= 0:
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw source reference is invalid',
                    error_code='context_plan_capacity_raw_source_invalid',
                )
            raw_members.append((
                member,
                user_id,
                assistant_id,
            ))
    raw_members.sort(key=lambda item: int(getattr(item[0], 'seq', 0) or 0))
    if not raw_members:
        raise CapacitySwapRuntimeError(
            'Capacity ContextPlan has no raw resume seed',
            error_code='context_plan_capacity_resume_seed_missing',
        )
    refs = [str(getattr(item[0], 'source_ref', '') or '') for item in raw_members]
    if len(refs) != len(set(refs)):
        raise CapacitySwapRuntimeError(
            'Capacity ContextPlan raw source is duplicated',
            error_code='context_plan_capacity_raw_source_duplicated',
        )
    return raw_members


def _capacity_context_candidate_session_id(
    *,
    plan_hash: str,
    source_context_id: int,
    source_context_epoch: int,
    source_resident_generation: int,
    source_scan_offset: int,
    trigger_reason: str,
) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        (
            'hayagarden-capacity-context-plan:'
            f'{str(plan_hash)}:'
            f'{int(source_context_id)}:'
            f'{int(source_context_epoch)}:'
            f'{int(source_resident_generation)}:'
            f'{int(source_scan_offset)}:'
            f'{str(trigger_reason)}'
        ),
    ))


def _capacity_context_source_member_from_db(
    *,
    conn: Any,
    context_id: int,
    context_epoch: int,
    user_id: int,
    assistant_id: int,
) -> Any:
    from continuity.sources import build_source_members, derive_completed_turns

    rows = conn.execute(
        'SELECT DISTINCT m.* FROM chat_messages m '
        'JOIN daily_message_contexts dmc ON dmc.message_id=m.id '
        'WHERE dmc.context_id=? AND dmc.context_epoch=? '
        'AND m.id IN (?,?) ORDER BY m.id ASC',
        (int(context_id), int(context_epoch), int(user_id), int(assistant_id)),
    ).fetchall()
    turns = tuple(derive_completed_turns(rows))
    expected_ref = 'turn:%d:%d' % (int(user_id), int(assistant_id))
    matched = tuple(turn for turn in turns if turn.turn_id == expected_ref)
    if len(matched) != 1:
        raise CapacitySwapRuntimeError(
            'ContextPlan raw source cannot be revalidated',
            error_code='context_plan_capacity_raw_identity_mismatch',
        )
    members = tuple(build_source_members(matched, ()))
    if len(members) != 1:
        raise CapacitySwapRuntimeError(
            'ContextPlan raw source identity is ambiguous',
            error_code='context_plan_capacity_raw_identity_mismatch',
        )
    return members[0]


def _capacity_context_source_identity(member: Any) -> tuple[Any, ...]:
    return (
        str(getattr(member, 'source_ref', '') or ''),
        str(getattr(member, 'source_revision', '') or ''),
        str(getattr(member, 'source_kind', '') or ''),
        str(getattr(member, 'content_hash', '') or ''),
        getattr(member, 'span_start', None),
        getattr(member, 'span_end', None),
        str(getattr(member, 'branch_id', '') or ''),
    )


def _capacity_context_emitted_event_uuids(
    graph: Any,
    rounds: Sequence[Any],
) -> list[str]:
    excluded_roles = {
        EventRole.SYSTEM,
        EventRole.SUMMARY,
        EventRole.META,
        EventRole.UNKNOWN,
        EventRole.SIDECHAIN,
        EventRole.USER_CONTINUATION,
    }
    out: list[str] = []
    seen: set[str] = set()
    for round_item in rounds:
        for event_uuid in tuple(getattr(round_item, 'event_uuids', ()) or ()):
            event_uuid = str(event_uuid)
            if event_uuid in seen:
                continue
            event = graph.by_uuid.get(event_uuid)
            if event is None:
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw round event is missing',
                    error_code='context_plan_capacity_raw_event_missing',
                )
            if (
                event.event_role in excluded_roles
                or bool(getattr(event, 'is_sidechain', False))
            ):
                continue
            out.append(event_uuid)
            seen.add(event_uuid)
    return out


def prepare_capacity_swap_for_context_plan(
    *,
    plan: Any,
    context_plan: Any,
    trigger_reason: str,
    static_system: str,
    claude_home: Optional[str] = None,
) -> CapacitySwapRuntimeResult:
    """Build an exact raw-only candidate from the consumed ContextPlan.

    This is the gate-on WHAT adapter. It deliberately does not call the
    legacy anchor/tail selector. The transport/publish/staged-resume path stays
    in run_capacity_swap_handoff().
    """
    reason = str(trigger_reason or '').strip()
    source_gen = int(plan.resident_generation)
    source_ctx = int(plan.context_id)
    source_epoch = int(plan.context_epoch)

    def _failure(code: str, warnings: Sequence[str] = ()) -> CapacitySwapRuntimeResult:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code=str(code),
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
            warnings=list(warnings),
        )

    if not is_capacity_swap_reason(reason):
        return _failure('trigger_not_capacity', [f'rejected_reason:{reason}'])

    try:
        raw_members = _context_plan_raw_members(context_plan)
    except CapacitySwapRuntimeError as exc:
        return _failure(exc.error_code, [str(exc)])

    registry = get_context_claude_session(
        source_ctx, source_gen, db_path=plan.db_path,
    )
    if registry is None:
        return _failure('registry_missing')

    from chat import daily_context as dc

    watermark = latest_complete_assistant_watermark(
        context_id=source_ctx,
        context_epoch=source_epoch,
        resident_generation=source_gen,
        before_message_id=int(plan.user_message_id),
        db_path=plan.db_path,
    )
    if registry_mapping_lags_watermark(registry, watermark):
        catchup = attempt_mapping_catchup_through(
            context_id=source_ctx,
            context_epoch=source_epoch,
            resident_generation=source_gen,
            chat_id=str(getattr(plan, 'chat_id', '') or 'default'),
            through_assistant_id=int(watermark),
            db_path=plan.db_path,
        )
        registry = get_context_claude_session(
            source_ctx, source_gen, db_path=plan.db_path,
        ) or registry
        if (
            not catchup.ok
            or registry_mapping_lags_watermark(registry, watermark)
        ):
            return _failure(
                ERROR_MAPPING_LAG,
                [
                    str(catchup.error_code or ERROR_MAPPING_LAG),
                    'watermark=%s' % watermark,
                    'last_mapped=%s' % registry.get('last_mapped_message_id'),
                ],
            )

    if str(registry.get('scan_status') or '') == SCAN_STATUS_BLOCKED:
        return _failure('registry_blocked')

    path = str(registry.get('transcript_path') or '').strip()
    sid = str(registry.get('claude_session_id') or '').strip()
    try:
        scan_offset = int(registry.get('scan_offset') or 0)
    except (TypeError, ValueError):
        scan_offset = 0
    if not path or not sid or scan_offset <= 0:
        return _failure('source_prefix_unreliable')

    try:
        source_sha = _snapshot_prefix_sha256(path, scan_offset)
        graph = read_transcript_range(path, 0, scan_offset)
    except Exception as exc:
        logger.info('capacity ContextPlan source read failed: %s', type(exc).__name__)
        return _failure('source_transcript_unreadable', [type(exc).__name__])

    conn = dc._connect(plan.db_path)
    try:
        formal = _load_formal_messages_excluding_current(
            conn,
            context_id=source_ctx,
            context_epoch=source_epoch,
            current_user_message_id=int(plan.user_message_id),
            cursor_before=plan.cursor_before,
        )
        mids = [int(m['id']) for m in formal]
        canonical, mid_to_event, event_to_mid = _load_mapping_for_messages(conn, mids)

        round_by_user: dict[str, Any] = {}
        for round_item in tuple(getattr(graph, 'candidate_rounds', ()) or ()):
            candidate_uuid = str(getattr(round_item, 'candidate_user_event_uuid', '') or '')
            if not candidate_uuid or candidate_uuid in round_by_user:
                raise CapacitySwapRuntimeError(
                    'Capacity source candidate round identity is ambiguous',
                    error_code='context_plan_capacity_raw_identity_mismatch',
                )
            round_by_user[candidate_uuid] = round_item

        selected_rounds: list[Any] = []
        selected_round_users: set[str] = set()
        for member, user_id, assistant_id in raw_members:
            if int(user_id) == int(plan.user_message_id):
                raise CapacitySwapRuntimeError(
                    'current user is present in the raw ContextPlan carrier',
                    error_code='context_plan_capacity_current_user_in_candidate',
                )
            user_event_uuid = str(mid_to_event.get(int(user_id)) or '').strip()
            if not user_event_uuid:
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw user mapping is missing',
                    error_code='context_plan_capacity_raw_mapping_missing',
                )
            user_event = graph.by_uuid.get(user_event_uuid)
            if user_event is None or user_event.event_role != EventRole.CANDIDATE_USER:
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw user mapping is not a candidate event',
                    error_code='context_plan_capacity_raw_mapping_invalid',
                )
            round_item = round_by_user.get(user_event_uuid)
            if round_item is None:
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw completed turn is absent from source graph',
                    error_code='context_plan_capacity_raw_round_missing',
                )
            if bool(getattr(round_item, 'has_sidechain_impact', False)):
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw completed turn has sidechain impact',
                    error_code='context_plan_capacity_raw_sidechain_invalid',
                )
            if user_event_uuid in selected_round_users:
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw round is duplicated',
                    error_code='context_plan_capacity_raw_source_duplicated',
                )
            assistant_events = [
                str(event_uuid)
                for event_uuid in tuple(getattr(round_item, 'event_uuids', ()) or ())
                if (
                    graph.by_uuid.get(str(event_uuid)) is not None
                    and graph.by_uuid[str(event_uuid)].event_role == EventRole.ASSISTANT
                    and int(event_to_mid.get(str(event_uuid), 0) or 0) == int(assistant_id)
                )
            ]
            if len(assistant_events) != 1:
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw assistant mapping is not exact',
                    error_code='context_plan_capacity_raw_mapping_invalid',
                )
            source_member = _capacity_context_source_member_from_db(
                conn=conn,
                context_id=source_ctx,
                context_epoch=source_epoch,
                user_id=user_id,
                assistant_id=assistant_id,
            )
            if (
                _capacity_context_source_identity(source_member)
                != _capacity_context_source_identity(member)
            ):
                raise CapacitySwapRuntimeError(
                    'ContextPlan raw source identity changed',
                    error_code='context_plan_capacity_raw_identity_mismatch',
                )
            selected_rounds.append(round_item)
            selected_round_users.add(user_event_uuid)

        selected_uuids = {
            str(getattr(round_item, 'candidate_user_event_uuid', '') or '')
            for round_item in selected_rounds
        }
        all_round_uuids = [
            str(getattr(round_item, 'candidate_user_event_uuid', '') or '')
            for round_item in tuple(getattr(graph, 'candidate_rounds', ()) or ())
        ]
        excluded_rounds = frozenset(
            uuid_value for uuid_value in all_round_uuids
            if uuid_value not in selected_uuids
        )
    except CapacitySwapRuntimeError as exc:
        return _failure(exc.error_code, [str(exc)])
    finally:
        conn.close()

    cid = _capacity_context_candidate_session_id(
        plan_hash=str(getattr(context_plan, 'plan_hash', '') or ''),
        source_context_id=source_ctx,
        source_context_epoch=source_epoch,
        source_resident_generation=source_gen,
        source_scan_offset=scan_offset,
        trigger_reason=reason,
    )
    cwd_guess = str(getattr(plan, 'transcript_cwd', '') or '')
    if not cwd_guess:
        cwd_guess = os.getcwd()
    request = TransformRequest(
        new_session_id=cid,
        cwd=cwd_guess,
        keep_rounds=len(selected_rounds),
        user_canonical_by_event_uuid=canonical,
        thinking_policy=ThinkingPolicy.DROP,
        sidechain_policy=SidechainPolicy.EXCLUDE,
        summary_policy=SummaryPolicy.DROP,
        unknown_event_policy=UnknownEventPolicy.DROP,
        selection_policy=SelectionPolicy.FIXED_ROUND_COUNT,
        exclude_round_candidate_uuids=excluded_rounds,
    )
    try:
        transformed = transform_transcript(graph, request)
    except TransformError as exc:
        return _failure(
            'context_plan_capacity_raw_transform_failed',
            [str(exc)],
        )

    if transformed.selected_round_count != len(selected_rounds):
        return _failure(
            'context_plan_capacity_raw_selection_changed',
            ['selected=%s expected=%s' % (
                transformed.selected_round_count, len(selected_rounds),
            )],
        )
    selected_user_uuids = {
        str(getattr(round_item, 'candidate_user_event_uuid', '') or '')
        for round_item in selected_rounds
    }
    if (
        selected_user_uuids
        & set(str(value) for value in transformed.dropped_sidechain_round_user_uuids)
    ) or (
        selected_user_uuids
        & set(str(value) for value in transformed.dropped_unconfirmed_user_uuids)
    ):
        return _failure(
            'context_plan_capacity_raw_selection_changed',
            ['selected round was dropped by transform'],
        )

    expected_events = _capacity_context_emitted_event_uuids(
        graph, selected_rounds,
    )
    actual_events = [str(value) for value in transformed.uuid_map.keys()]
    if actual_events != expected_events:
        return _failure(
            'context_plan_capacity_raw_identity_mismatch',
            ['native event set differs from exact ContextPlan rounds'],
        )

    serialized = serialize_events(transformed.events)
    validation = validate_transcript_events(
        transformed.events,
        ValidatorOptions(
            session_id=cid,
            thinking_policy=ThinkingPolicy.DROP,
            forbid_sidechain=True,
            forbid_summary=True,
            expected_round_count=len(selected_rounds),
            max_round_count=len(selected_rounds),
            old_uuids=set(graph.by_uuid.keys()),
            unknown_event_mode='reject',
        ),
    )
    if not validation.ok:
        return _failure(
            'context_plan_capacity_raw_validator_rejected',
            list(validation.errors),
        )

    selected_message_ids: list[int] = []
    for event_uuid in expected_events:
        event = graph.by_uuid.get(event_uuid)
        message_id = int(event_to_mid.get(event_uuid, 0) or 0)
        if message_id <= 0 and (
            event is None or event.event_role != EventRole.TOOL_RESULT_USER
        ):
            return _failure(
                'context_plan_capacity_raw_mapping_missing',
                ['native event mapping missing:%s' % event_uuid],
            )
        if message_id == int(plan.user_message_id):
            return _failure(
                'context_plan_capacity_current_user_in_candidate',
            )
        if message_id > 0 and message_id not in selected_message_ids:
            selected_message_ids.append(message_id)

    serialized_bytes = len(serialized.encode('utf-8'))
    candidate = CapacitySwapCandidate(
        source_context_id=source_ctx,
        source_context_epoch=source_epoch,
        source_resident_generation=source_gen,
        source_claude_session_id=sid,
        source_transcript_path=path,
        source_scan_offset=scan_offset,
        source_sha256=source_sha,
        target_resident_generation=source_gen + 1,
        candidate_session_id=cid,
        trigger_reason=reason,
        anchor_status=AnchorStatus.ANCHOR_UNAVAILABLE,
        anchor_message_id=0,
        anchor_event_uuid=None,
        selected_round_count=len(selected_rounds),
        selected_message_ids=tuple(selected_message_ids),
        estimated_tokens=estimate_serialized_token_count(transformed.events),
        serialized_bytes=serialized_bytes,
        event_count=len(transformed.events),
        output_sha256=sha256_text(serialized),
        serialized_jsonl=serialized,
        boundary_required=True,
        warnings=('context_plan_exact_raw',),
    )
    return CapacitySwapRuntimeResult(
        ok=True,
        candidate=candidate,
        source_context_id=source_ctx,
        source_context_epoch=source_epoch,
        source_resident_generation=source_gen,
        target_resident_generation=source_gen + 1,
        candidate_session_id=cid,
        effective_system=with_capacity_boundary_suffix(static_system),
        warnings=['context_plan_exact_raw'],
    )

def prepare_capacity_swap_for_plan(
    *,
    plan: Any,
    trigger_reason: str,
    static_system: str,
    claude_home: Optional[str] = None,
) -> CapacitySwapRuntimeResult:
    """Build + validate an unpublished candidate for the current plan (no spawn)."""
    if not is_capacity_swap_reason(trigger_reason):
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='trigger_not_capacity',
            warnings=[f'rejected_reason:{trigger_reason}'],
        )

    source_gen = int(plan.resident_generation)
    source_ctx = int(plan.context_id)
    source_epoch = int(plan.context_epoch)
    registry = get_context_claude_session(
        source_ctx, source_gen, db_path=plan.db_path,
    )
    if registry is None:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='registry_missing',
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
        )
    # Stale reliable-prefix guard (Step 10 Defect B): never build a candidate
    # from a READY-but-lagging mapped prefix. Attempt synchronous catch-up
    # first; if still behind the authoritative formal watermark, fail closed.
    from chat import daily_context as dc

    watermark = latest_complete_assistant_watermark(
        context_id=source_ctx,
        context_epoch=source_epoch,
        resident_generation=source_gen,
        before_message_id=int(plan.user_message_id),
        db_path=plan.db_path,
    )
    if registry_mapping_lags_watermark(registry, watermark):
        catchup = attempt_mapping_catchup_through(
            context_id=source_ctx,
            context_epoch=source_epoch,
            resident_generation=source_gen,
            chat_id=str(getattr(plan, 'chat_id', '') or 'default'),
            through_assistant_id=int(watermark),
            db_path=plan.db_path,
        )
        registry = get_context_claude_session(
            source_ctx, source_gen, db_path=plan.db_path,
        ) or registry
        if (
            not catchup.ok
            or registry_mapping_lags_watermark(registry, watermark)
        ):
            return CapacitySwapRuntimeResult(
                ok=False,
                error_code=ERROR_MAPPING_LAG,
                source_context_id=source_ctx,
                source_context_epoch=source_epoch,
                source_resident_generation=source_gen,
                warnings=[
                    str(catchup.error_code or ERROR_MAPPING_LAG),
                    f'watermark={watermark}',
                    f"last_mapped={registry.get('last_mapped_message_id')}",
                ],
            )

    if str(registry.get('scan_status') or '') == SCAN_STATUS_BLOCKED:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='registry_blocked',
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
        )

    path = str(registry.get('transcript_path') or '').strip()
    sid = str(registry.get('claude_session_id') or '').strip()
    try:
        scan_offset = int(registry.get('scan_offset') or 0)
    except (TypeError, ValueError):
        scan_offset = 0
    if not path or not sid or scan_offset <= 0:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='source_prefix_unreliable',
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
        )

    try:
        source_sha = _snapshot_prefix_sha256(path, scan_offset)
        graph = read_transcript_range(path, 0, scan_offset)
    except Exception as exc:
        logger.info('capacity swap source read failed: %s', type(exc).__name__)
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='source_transcript_unreadable',
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
            warnings=[type(exc).__name__],
        )

    conn = dc._connect(plan.db_path)
    try:
        formal = _load_formal_messages_excluding_current(
            conn,
            context_id=source_ctx,
            context_epoch=source_epoch,
            current_user_message_id=int(plan.user_message_id),
            cursor_before=plan.cursor_before,
        )
        mids = [int(m['id']) for m in formal]
        canonical, mid_to_event, event_to_mid = _load_mapping_for_messages(conn, mids)
    finally:
        conn.close()

    if not formal or not canonical:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='mapping_insufficient',
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
        )

    effective_system = with_capacity_boundary_suffix(static_system)
    state_text = str((getattr(plan, 'assembly', {}) or {}).get('state') or '')
    retained = compute_retained_transcript_token_budget(
        static_system=effective_system,
        pending_user_text=str(getattr(plan, 'user_content', '') or ''),
        dynamic_state_text=state_text,
    )
    anchor_budget = compute_anchor_token_budget()

    cwd_guess = str(getattr(plan, 'transcript_cwd', '') or '')
    if not cwd_guess:
        cwd_guess = os.getcwd()

    prep: CapacitySwapPrepareResult = prepare_capacity_swap_candidate(
        graph=graph,
        trigger_reason=str(trigger_reason),
        source_context_id=source_ctx,
        source_context_epoch=source_epoch,
        source_resident_generation=source_gen,
        source_claude_session_id=sid,
        source_transcript_path=path,
        source_scan_offset=scan_offset,
        source_sha256=source_sha,
        formal_messages=formal,
        user_canonical_by_event_uuid=canonical,
        mapping_event_uuid_by_message_id=mid_to_event,
        mapping_message_id_by_event_uuid=event_to_mid,
        retained_transcript_token_budget=retained,
        anchor_token_budget=anchor_budget,
        cwd=cwd_guess,
        thinking_policy=ThinkingPolicy.DROP,
    )
    if prep.status not in {CapacitySwapStatus.READY, CapacitySwapStatus.TAIL_BUDGET_EXCEEDED}:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code=str(prep.status.value),
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
            warnings=list(prep.warnings),
        )
    if prep.candidate is None:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='candidate_missing',
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
        )
    if prep.status == CapacitySwapStatus.TAIL_BUDGET_EXCEEDED and prep.candidate.event_count == 0:
        # Anchor-only may be empty of tail; still require at least one event for resume.
        if prep.candidate.event_count <= 0:
            return CapacitySwapRuntimeResult(
                ok=False,
                error_code='tail_budget_exceeded_empty',
                candidate=prep.candidate,
                source_context_id=source_ctx,
                source_context_epoch=source_epoch,
                source_resident_generation=source_gen,
                warnings=list(prep.warnings),
            )

    cand = prep.candidate
    try:
        assert_current_user_excluded(
            candidate=cand,
            current_user_message_id=int(plan.user_message_id),
            current_user_content=str(getattr(plan, 'user_content', '') or ''),
        )
    except CapacitySwapRuntimeError as exc:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code=exc.error_code,
            candidate=cand,
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
            current_user_in_candidate=True,
        )

    # Boundary must not appear as forged JSONL events.
    blob = cand.serialized_jsonl or ''
    if '[容量边界]' in blob or '[context-window-boundary]' in blob:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='boundary_leaked_into_jsonl',
            candidate=cand,
            source_context_id=source_ctx,
            source_context_epoch=source_epoch,
            source_resident_generation=source_gen,
        )

    return CapacitySwapRuntimeResult(
        ok=True,
        candidate=cand,
        source_context_id=source_ctx,
        source_context_epoch=source_epoch,
        source_resident_generation=source_gen,
        target_resident_generation=int(cand.target_resident_generation),
        candidate_session_id=cand.candidate_session_id,
        effective_system=effective_system,
        warnings=list(prep.warnings),
    )


def publish_capacity_swap_candidate_jsonl(
    candidate: CapacitySwapCandidate,
    *,
    cwd: str,
    claude_home: Optional[str] = None,
) -> tuple[Path, str]:
    """Atomic publish of candidate JSONL with 0600 + allowed-root fence.

    Source transcript is never written. Does not change Manual Forge defaults.
    """
    events = [
        json.loads(line)
        for line in (candidate.serialized_jsonl or '').splitlines()
        if line.strip()
    ]
    if not events:
        raise CapacitySwapRuntimeError('empty candidate jsonl', error_code='empty_candidate')
    if events[0].get('type') != 'user':
        raise CapacitySwapRuntimeError(
            'candidate must start with user',
            error_code='candidate_first_not_user',
        )
    if any(e.get('type') == 'system' for e in events):
        raise CapacitySwapRuntimeError(
            'system events forbidden in capacity swap jsonl',
            error_code='boundary_system_event_forbidden',
        )

    home = Path(claude_home) if claude_home else (Path.home() / '.claude')
    try:
        from tools.claude_forge_core import verify_work_root, verify_safe_output_path
        allowed_root = verify_work_root(home)
    except Exception as exc:
        raise CapacitySwapRuntimeError(
            f'capacity swap allowed root rejected: {exc}',
            error_code='publish_path_fence_failed',
        ) from exc

    out_path = Path(derive_transcript_path(
        cwd=cwd,
        claude_session_id=candidate.candidate_session_id,
        claude_home=str(home),
    ))
    try:
        verify_safe_output_path(out_path, allowed_root)
    except Exception as exc:
        raise CapacitySwapRuntimeError(
            f'capacity swap output path rejected: {exc}',
            error_code='publish_path_fence_failed',
        ) from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out_path.parent, 0o700)
    except OSError:
        pass

    digest = atomic_write_jsonl_fsync(
        out_path,
        events,
        mode=0o600,
        allowed_root=allowed_root,
    )
    mode = out_path.stat().st_mode & 0o777
    if mode & ~0o600:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CapacitySwapRuntimeError(
            f'capacity swap jsonl mode {oct(mode)} violates 0600',
            error_code='publish_mode_not_0600',
        )

    try:
        from tools.claude_forge_core import sha256_file as _sha
        file_sha = _sha(out_path)
    except Exception:
        import hashlib
        file_sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
    if file_sha != digest:
        raise CapacitySwapRuntimeError('publish sha mismatch', error_code='publish_sha_mismatch')
    return out_path, file_sha


@dataclass
class CapacitySwapStagedHooks:
    """Injectable staged spawn/health for tests (production uses real ResidentSession)."""

    spawn_and_health: Callable[..., Any]
    # spawn_and_health(effective_system, env, *, resume_session_id, jsonl_path, expected_sha256) -> staged


def run_capacity_swap_handoff(
    *,
    plan: Any,
    trigger_reason: str,
    static_system: str,
    env: Mapping[str, str],
    live_resident: Any,
    staged_hooks: CapacitySwapStagedHooks,
    claude_home: Optional[str] = None,
    prepare_result: Optional[CapacitySwapRuntimeResult] = None,
) -> CapacitySwapRuntimeResult:
    """Publish + staged resume + health. Does NOT send CURRENT user. Old stays alive until return."""
    prep = prepare_result or prepare_capacity_swap_for_plan(
        plan=plan,
        trigger_reason=trigger_reason,
        static_system=static_system,
        claude_home=claude_home,
    )
    if not prep.ok or prep.candidate is None:
        return prep

    cand = prep.candidate
    cwd = str(getattr(live_resident, 'cwd', '') or getattr(plan, 'transcript_cwd', '') or '')
    if not cwd:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='cwd_missing',
            candidate=cand,
            source_context_id=prep.source_context_id,
            source_context_epoch=prep.source_context_epoch,
            source_resident_generation=prep.source_resident_generation,
        )

    try:
        jsonl_path, file_sha = publish_capacity_swap_candidate_jsonl(
            cand, cwd=cwd, claude_home=claude_home,
        )
    except CapacitySwapRuntimeError as exc:
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code=exc.error_code,
            candidate=cand,
            source_context_id=prep.source_context_id,
            source_context_epoch=prep.source_context_epoch,
            source_resident_generation=prep.source_resident_generation,
        )

    effective_system = with_capacity_boundary_suffix(static_system)
    try:
        staged = staged_hooks.spawn_and_health(
            effective_system,
            dict(env),
            resume_session_id=cand.candidate_session_id,
            jsonl_path=jsonl_path,
            expected_sha256=file_sha,
        )
    except Exception as exc:
        logger.info('capacity swap staged failed: %s', type(exc).__name__)
        return CapacitySwapRuntimeResult(
            ok=False,
            error_code='staged_health_failed',
            candidate=cand,
            source_context_id=prep.source_context_id,
            source_context_epoch=prep.source_context_epoch,
            source_resident_generation=prep.source_resident_generation,
            jsonl_path=str(jsonl_path),
            jsonl_sha256=file_sha,
            effective_system=effective_system,
            warnings=[f'{type(exc).__name__}:{exc}'],
        )

    # Attach staged handle for caller install (attribute, not identity field).
    prep_out = CapacitySwapRuntimeResult(
        ok=True,
        candidate=cand,
        source_context_id=prep.source_context_id,
        source_context_epoch=prep.source_context_epoch,
        source_resident_generation=prep.source_resident_generation,
        target_resident_generation=int(cand.target_resident_generation),
        candidate_session_id=cand.candidate_session_id,
        jsonl_path=str(jsonl_path),
        jsonl_sha256=file_sha,
        effective_system=effective_system,
        warnings=list(prep.warnings),
    )
    setattr(prep_out, 'staged_resident', staged)
    return prep_out


def register_capacity_swap_generation(
    *,
    plan: Any,
    candidate: CapacitySwapCandidate,
    process_generation: int,
    scan_offset: int,
    cwd: str,
    claude_home: Optional[str] = None,
) -> dict[str, Any]:
    """Register target generation with source=capacity_swap (same context/epoch)."""
    return register_context_claude_session(
        context_id=int(plan.context_id),
        context_epoch=int(plan.context_epoch),
        resident_generation=int(candidate.target_resident_generation),
        chat_id=str(plan.chat_id),
        claude_session_id=str(candidate.candidate_session_id),
        cwd=cwd,
        source=CAPACITY_SWAP_REGISTRY_SOURCE,
        scan_offset=int(scan_offset),
        process_generation=int(process_generation),
        claude_home=claude_home,
        db_path=plan.db_path,
    )
