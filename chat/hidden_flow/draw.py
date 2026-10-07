"""Stable, side-effect-free pool draws and bounded private guide rendering."""

from __future__ import annotations

import hashlib
import json
from html import escape
from typing import Any, Iterable, Mapping, Optional

from .types import DrawItem, DrawResult, FlowConfig, FlowState, PoolSpec


MAX_DRAWS = 12
MAX_PRIVATE_TEXT = 8000


def _seed(parts: Iterable[object]) -> str:
    values = [str(part) for part in parts]
    if any("\0" in value for value in values):
        raise ValueError("NUL is not allowed in draw identity")
    payload = "\0".join(values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pool_seed(
    pool: PoolSpec,
    *,
    source_message_id: object,
    flow_id: str,
    cycle: int,
    flow_started_at: str,
    draw_index: int,
) -> str:
    if pool.draw_mode == "cycle":
        return _seed((flow_started_at, flow_id, cycle, pool.pool_id, draw_index))
    return _seed((source_message_id, flow_id, pool.pool_id, draw_index))


def draw_pool(
    pool: PoolSpec,
    *,
    source_message_id: object = "",
    flow_id: str = "",
    cycle: int = 1,
    flow_started_at: str = "",
    draw_index: int = 0,
) -> DrawResult:
    """Draw without replacement using hash ordering instead of runtime state."""
    if not pool.enabled or not pool.entries or not flow_id:
        return DrawResult()
    if pool.draw_mode == "turn" and not str(source_message_id):
        return DrawResult()
    if pool.draw_mode == "cycle" and not flow_started_at:
        return DrawResult()
    selected_ids: set[str] = set()
    draws: list[DrawItem] = []
    try:
        for offset in range(pool.draw_count):
            slot_index = draw_index + offset
            seed = _pool_seed(
                pool,
                source_message_id=source_message_id,
                flow_id=flow_id,
                cycle=cycle,
                flow_started_at=flow_started_at,
                draw_index=slot_index,
            )
            ranked = sorted(
                pool.entries,
                key=lambda entry: (
                    hashlib.sha256((seed + "\0" + entry.entry_id).encode("utf-8")).hexdigest(),
                    entry.entry_id,
                ),
            )
            entry = next((item for item in ranked if item.entry_id not in selected_ids), None)
            if entry is None:
                break
            selected_ids.add(entry.entry_id)
            draws.append(
                DrawItem(
                    pool_id=pool.pool_id,
                    entry_id=entry.entry_id,
                    text=entry.text,
                    draw_index=slot_index,
                    seed=seed,
                )
            )
    except ValueError:
        return DrawResult()
    return DrawResult(tuple(draws), text_chars=sum(len(item.text) for item in draws))


def draw_for_guide(
    config: FlowConfig,
    state: FlowState,
    *,
    source_message_id: object,
    cue_keys: Iterable[str] = (),
    pool_ids: Iterable[str] = (),
) -> DrawResult:
    """Draw stage/cue pools once each, with hard composite caps."""
    result, _ = _draw_for_guide_with_state(
        config,
        state,
        source_message_id=source_message_id,
        cue_keys=cue_keys,
        pool_ids=pool_ids,
        use_fixed_draws=False,
    )
    return result


def _selected_pool_ids(
    config: FlowConfig,
    state: FlowState,
    cue_keys: Iterable[str],
    pool_ids: Iterable[str],
) -> list[str]:
    stage = config.stage(state.stage)
    if stage is None:
        return []
    selected_ids: list[str] = list(stage.pool_ids)
    for key in cue_keys:
        cue = config.cue_for_key(key)
        if cue is not None:
            selected_ids.extend(cue.pool_ids)
    selected_ids.extend(str(item).strip() for item in pool_ids if str(item).strip())
    return selected_ids


def _cached_cycle_draw(
    pool: PoolSpec,
    cached: Any,
    *,
    flow_id: str,
    cycle: int,
    flow_started_at: str,
    draw_index: int,
) -> Optional[DrawResult]:
    """Accept only a bounded cache tied to current normalized entries and seeds."""
    if pool.draw_mode != "cycle" or not isinstance(cached, list) or not cached:
        return None
    if len(cached) > pool.draw_count:
        return None
    entries = {entry.entry_id: entry for entry in pool.entries}
    seen_entries: set[str] = set()
    seen_indexes: set[int] = set()
    draws: list[DrawItem] = []
    for raw in cached:
        if not isinstance(raw, Mapping):
            return None
        pool_id = raw.get("poolId")
        entry_id = raw.get("entryId")
        text = raw.get("text")
        item_index = raw.get("drawIndex")
        seed = raw.get("seed")
        if (
            pool_id != pool.pool_id
            or not isinstance(entry_id, str)
            or not isinstance(text, str)
            or not isinstance(item_index, int)
            or isinstance(item_index, bool)
            or not isinstance(seed, str)
            or entry_id in seen_entries
            or item_index in seen_indexes
            or item_index < draw_index
            or item_index >= draw_index + pool.draw_count
        ):
            return None
        entry = entries.get(entry_id)
        if entry is None or entry.text != text:
            return None
        expected_seed = _pool_seed(
            pool,
            source_message_id="",
            flow_id=flow_id,
            cycle=cycle,
            flow_started_at=flow_started_at,
            draw_index=item_index,
        )
        if seed != expected_seed:
            return None
        seen_entries.add(entry_id)
        seen_indexes.add(item_index)
        draws.append(DrawItem(pool.pool_id, entry_id, text, item_index, seed))
    if not draws or sum(len(item.text) for item in draws) > MAX_PRIVATE_TEXT:
        return None
    expected = draw_pool(
        pool,
        flow_id=flow_id,
        cycle=cycle,
        flow_started_at=flow_started_at,
        draw_index=draw_index,
    )
    if tuple(item.to_dict() for item in draws) != tuple(item.to_dict() for item in expected.draws):
        return None
    return DrawResult(tuple(draws), text_chars=sum(len(item.text) for item in draws))


def _copy_fixed_draws(state: FlowState) -> dict[str, list[dict[str, Any]]]:
    copied: dict[str, list[dict[str, Any]]] = {}
    for pool_id, raw_items in state.fixed_draws.items():
        if not isinstance(pool_id, str) or not isinstance(raw_items, list):
            continue
        if not all(isinstance(item, Mapping) for item in raw_items):
            continue
        copied[pool_id] = [dict(item) for item in raw_items]
    try:
        json.dumps(copied, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return {}
    return copied


def _draw_for_guide_with_state(
    config: FlowConfig,
    state: FlowState,
    *,
    source_message_id: object,
    cue_keys: Iterable[str],
    pool_ids: Iterable[str],
    use_fixed_draws: bool,
) -> tuple[DrawResult, FlowState]:
    """Draw and return an explicit state copy with cycle caches applied."""
    if not config.enabled or not state.active:
        return DrawResult(), state
    selected_ids = _selected_pool_ids(config, state, cue_keys, pool_ids)
    if not selected_ids:
        return DrawResult(), state
    result: list[DrawItem] = []
    seen_pools: set[str] = set()
    truncated = False
    truncation_reason: Optional[str] = None
    fixed_draws = _copy_fixed_draws(state) if use_fixed_draws else dict(state.fixed_draws)
    fixed_count = sum(len(items) for items in fixed_draws.values() if isinstance(items, list))
    fixed_text_chars = sum(
        len(item.get("text", ""))
        for items in fixed_draws.values()
        if isinstance(items, list)
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("text"), str)
    )
    for pool_id in selected_ids:
        if pool_id in seen_pools:
            continue
        seen_pools.add(pool_id)
        pool = config.pool(pool_id)
        if pool is None:
            continue
        drawn = None
        cache_hit = False
        if use_fixed_draws and pool.draw_mode == "cycle":
            drawn = _cached_cycle_draw(
                pool,
                fixed_draws.get(pool.pool_id),
                flow_id=config.flow_id,
                cycle=state.cycle,
                flow_started_at=state.started_at,
                draw_index=0,
            )
            cache_hit = drawn is not None
        if drawn is None:
            if use_fixed_draws and pool.draw_mode == "cycle":
                fixed_draws.pop(pool.pool_id, None)
            drawn = draw_pool(
                pool,
                source_message_id=source_message_id,
                flow_id=config.flow_id,
                cycle=state.cycle,
                flow_started_at=state.started_at,
                draw_index=0,
            )
        accepted: list[DrawItem] = []
        pool_complete = True
        for item in drawn.draws:
            if len(result) >= MAX_DRAWS:
                truncated = True
                truncation_reason = "draw_cap"
                pool_complete = False
                break
            current_size = sum(len(entry.text) for entry in result)
            if current_size + len(item.text) > MAX_PRIVATE_TEXT:
                truncated = True
                truncation_reason = "text_cap"
                pool_complete = False
                break
            result.append(item)
            accepted.append(item)
        if use_fixed_draws and pool.draw_mode == "cycle" and not cache_hit:
            old_items = fixed_draws.get(pool.pool_id, [])
            old_text_chars = sum(
                len(item.get("text", ""))
                for item in old_items
                if isinstance(item, Mapping) and isinstance(item.get("text"), str)
            )
            fixed_count -= len(old_items)
            fixed_text_chars -= old_text_chars
            if accepted and pool_complete and len(accepted) == len(drawn.draws) and fixed_count + len(accepted) <= MAX_DRAWS and fixed_text_chars + sum(
                len(item.text) for item in accepted
            ) <= MAX_PRIVATE_TEXT:
                fixed_draws[pool.pool_id] = [item.to_dict() for item in accepted]
                fixed_count += len(accepted)
                fixed_text_chars += sum(len(item.text) for item in accepted)
            else:
                fixed_draws.pop(pool.pool_id, None)
        if len(result) >= MAX_DRAWS:
            break
        if sum(len(entry.text) for entry in result) >= MAX_PRIVATE_TEXT:
            break
    text_chars = sum(len(entry.text) for entry in result)
    reason = truncation_reason if truncated else None
    updated = FlowState(
        active=state.active,
        flow_id=state.flow_id,
        stage=state.stage,
        cycle=state.cycle,
        stage_turn=state.stage_turn,
        context_keys=state.context_keys,
        fixed_draws=fixed_draws,
        started_at=state.started_at,
        schema_version=state.schema_version,
    )
    return DrawResult(tuple(result), truncated, reason, text_chars), updated


def draw_for_guide_with_state(
    config: FlowConfig,
    state: FlowState,
    *,
    source_message_id: object,
    cue_keys: Iterable[str] = (),
    pool_ids: Iterable[str] = (),
) -> tuple[DrawResult, FlowState]:
    """Draw with explicit immutable-style cycle cache state propagation."""
    return _draw_for_guide_with_state(
        config,
        state,
        source_message_id=source_message_id,
        cue_keys=cue_keys,
        pool_ids=pool_ids,
        use_fixed_draws=True,
    )


def render_private_guide(
    config: FlowConfig,
    state: FlowState,
    draw_result: Optional[DrawResult] = None,
    protocol_hints: Iterable[str] = (),
) -> str:
    """Render only bounded normalized state and selected anchors."""
    if not config.enabled or not state.active or config.stage(state.stage) is None:
        return ""
    result = draw_result or DrawResult()
    safe_flow = escape(str(config.flow_id)[:256], quote=True)
    safe_stage = escape(str(state.stage)[:256], quote=True)
    chunks = [
        '<hidden_flow_guidance flow="%s" stage="%s" cycle="%d" turn="%d">'
        % (
            safe_flow,
            safe_stage,
            int(state.cycle),
            int(state.stage_turn),
        )
    ]
    used = len(chunks[0])
    for item in result.draws[:MAX_DRAWS]:
        pool = config.pool(item.pool_id)
        if pool is None or not any(entry.entry_id == item.entry_id and entry.text == item.text for entry in pool.entries):
            continue
        value = escape(item.text, quote=False)
        block = "<anchor pool=\"%s\" id=\"%s\">%s</anchor>" % (
            escape(str(item.pool_id)[:256], quote=True),
            escape(str(item.entry_id)[:256], quote=True),
            value,
        )
        if used + len(block) + len("</hidden_flow_guidance>") > MAX_PRIVATE_TEXT:
            break
        chunks.append(block)
        used += len(block)
    for hint in list(protocol_hints)[:4]:
        block = "<hint>%s</hint>" % escape(str(hint or "")[:1200], quote=False)
        if used + len(block) + len("</hidden_flow_guidance>") > MAX_PRIVATE_TEXT:
            break
        chunks.append(block)
        used += len(block)
    chunks.append("</hidden_flow_guidance>")
    return "".join(chunks)
