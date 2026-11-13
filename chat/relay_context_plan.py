"""Encode the canonical Continuity ContextPlan for stateless API Relay turns."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Callable


class RelayContextPlanError(RuntimeError):
    pass


def enabled() -> bool:
    try:
        import config_store
        return config_store.get_bool('CONTEXT_PLAN_CONSUMER_ENABLED', False)
    except Exception:
        return False


def bind_current_user(*, message_id: int, db_path: str) -> dict[str, Any]:
    from chat.daily_context import record_daily_message_context
    from chat.window_identity import capture_current_window_identity
    identity = capture_current_window_identity(db_path=db_path)
    record_daily_message_context(
        int(message_id), context_id=int(identity['context_id']),
        context_epoch=int(identity['context_epoch']),
        resident_generation=int(identity['resident_generation']),
        role='user', db_path=db_path,
    )
    return identity


def bind_assistant(*, message_id: int, identity: dict[str, Any], db_path: str) -> None:
    from chat.daily_context import record_daily_message_context
    record_daily_message_context(
        int(message_id), context_id=int(identity['context_id']),
        context_epoch=int(identity['context_epoch']),
        resident_generation=int(identity['resident_generation']),
        role='assistant', db_path=db_path,
    )


def _load_rows(db_path: str, ids: set[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        marks = ','.join('?' for _ in ids)
        rows = conn.execute(
            'SELECT * FROM chat_messages WHERE id IN (%s)' % marks,
            tuple(sorted(ids)),
        ).fetchall()
        return {int(row['id']): dict(row) for row in rows}
    finally:
        conn.close()


def _append(target: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> None:
    for msg in incoming:
        if not msg.get('content'):
            continue
        role = str(msg.get('role') or '')
        if not target or target[-1].get('role') != role:
            target.append(dict(msg))
            continue
        left, right = target[-1].get('content'), msg.get('content')
        if isinstance(left, str) and isinstance(right, str):
            target[-1]['content'] = left + '\n' + right
            continue
        if isinstance(left, str):
            left = [{'type': 'text', 'text': left}]
        if isinstance(right, str):
            right = [{'type': 'text', 'text': right}]
        target[-1]['content'] = list(left or []) + list(right or [])


def build_messages(
    *, user_message_id: int, db_path: str, system: Any, static_dir: str,
    read_file_fn: Callable[[str, str], str | None],
    img_block_fn: Callable[[str], dict[str, Any] | None],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    from chat import daily_runtime as daily_rt
    from chat.context_lean import lean_tool_budget_enabled
    from chat.daily_continuity_shadow import build_daily_continuity_shadow_plan
    from chat.history_assembly import assemble_history_from_rows, strip_internal_metadata
    from continuity.context_plan import ContextSection
    from continuity.store import read_ready_surface
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1

    user_message_id = int(user_message_id)
    identity = bind_current_user(message_id=user_message_id, db_path=db_path)
    current = _load_rows(db_path, {user_message_id}).get(user_message_id)
    if current is None:
        raise RelayContextPlanError('relay_context_current_user_missing')
    if isinstance(system, list):
        stable_system = '\n'.join(
            str(block.get('text') or '') for block in system
            if isinstance(block, dict) and block.get('type') == 'text'
        )
    else:
        stable_system = str(system or '')
    system_hash = hashlib.sha256(stable_system.encode()).hexdigest()
    fixed = (ContextSection(
        kind='invariant_system', source_ref='system:%s' % system_hash[:32],
        content_hash=system_hash,
        estimated_tokens=int(estimate_tokens_heuristic_cjk1_ascii4_v1(stable_system)),
    ),)
    policy, policy_version = daily_rt._context_plan_policy(
        mode='cold', static_system=stable_system,
        pending_user_text=str(current.get('content') or ''),
    )
    surface = read_ready_surface(db_path)
    if surface.status != 'ready':
        raise RelayContextPlanError(
            'relay_context_store_%s:%s' % (surface.status, surface.error_code or '')
        )
    artifacts = tuple(surface.artifacts or ())
    result = build_daily_continuity_shadow_plan(
        source_db_path=db_path, shadow_store_path=db_path,
        continuity_store_path=db_path, current_user_message_id=user_message_id,
        budget_policy=policy, accepted_fixed_sections=fixed,
        budget_policy_version=policy_version,
        validated_ready_artifacts=tuple({
            'chunk_id': str(chunk.chunk_id),
            'artifact_revision': str(chunk.artifact_revision),
            'body_hash': str(chunk.body_hash),
        } for chunk in artifacts),
    )
    plan = result.plan
    if result.status != 'ready' or plan is None:
        raise RelayContextPlanError(
            'relay_context_plan_unavailable:%s' % (result.error_code or result.status)
        )
    if not plan.valid:
        raise RelayContextPlanError('relay_context_plan_invalid:%s' % plan.budget_status)

    chunk_bodies = {str(chunk.chunk_id): str(chunk.body) for chunk in artifacts}
    wanted = {user_message_id}
    for rep in plan.representations:
        if rep.kind == 'raw':
            for member in rep.source_members:
                ids = daily_rt._context_source_message_ids(member.source_ref)
                if not ids:
                    raise RelayContextPlanError('relay_context_source_ref_invalid')
                wanted.update(ids)
    rows = _load_rows(db_path, wanted)

    def render(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not selected:
            return []
        rendered, _ = assemble_history_from_rows(
            selected, available_count=len(selected), history_token_budget=0,
            history_mode='context_plan', relay_high_water=0, relay_low_water=0,
            relay_head_id=0, static_dir=static_dir, read_file_fn=read_file_fn,
            img_block_fn=img_block_fn,
            is_ai_author=lambda author: author in ('fyodor', 'claude', 'assistant'),
            resident_file_hashes=set(), apply_tool_budget=lean_tool_budget_enabled(),
        )
        return strip_internal_metadata(rendered)

    messages: list[dict[str, Any]] = []
    seen: set[int] = set()
    for rep in plan.representations:
        if rep.kind == 'chunk':
            body = str(chunk_bodies.get(str(rep.chunk_id)) or '').strip()
            if not body:
                raise RelayContextPlanError('relay_context_chunk_missing')
            _append(messages, [{'role': 'assistant', 'content': body}])
            continue
        selected = []
        for member in rep.source_members:
            for message_id in daily_rt._context_source_message_ids(member.source_ref):
                if message_id == user_message_id or message_id in seen:
                    continue
                row = rows.get(message_id)
                if row is None:
                    raise RelayContextPlanError('relay_context_source_row_missing')
                seen.add(message_id)
                selected.append(row)
        _append(messages, render(selected))
    current = rows.get(user_message_id)
    if current is None:
        raise RelayContextPlanError('relay_context_current_user_missing')
    _append(messages, render([current]))
    if not messages or messages[-1].get('role') != 'user':
        raise RelayContextPlanError('relay_context_current_user_not_last')
    if messages[0].get('role') == 'assistant':
        messages.insert(0, {'role': 'user', 'content': '...'})
    return messages, identity, {
        'plan_id': str(plan.plan_id), 'plan_hash': str(plan.plan_hash),
        'source_hash': str(plan.source_hash), 'budget_status': str(plan.budget_status),
        'chunk_count': sum(rep.kind == 'chunk' for rep in plan.representations),
        'raw_count': sum(rep.kind == 'raw' for rep in plan.representations),
    }
