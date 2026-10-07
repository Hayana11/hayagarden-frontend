"""Stable, side-effect-free pool draws and bounded private guide rendering."""

from __future__ import annotations

import hashlib
from html import escape
from typing import Iterable, Optional

from .types import DrawItem, DrawResult, FlowConfig, FlowState, PoolSpec


MAX_DRAWS = 12
MAX_PRIVATE_TEXT = 8000


def _seed(parts: Iterable[object]) -> str:
    payload = "\0".join(str(part) for part in parts)
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
    if not pool.enabled or not pool.entries:
        return DrawResult()
    seed = _pool_seed(
        pool,
        source_message_id=source_message_id,
        flow_id=flow_id,
        cycle=cycle,
        flow_started_at=flow_started_at,
        draw_index=draw_index,
    )
    ranked = sorted(
        pool.entries,
        key=lambda entry: hashlib.sha256((seed + "\0" + entry.entry_id).encode("utf-8")).hexdigest(),
    )
    selected = ranked[: min(pool.draw_count, len(ranked))]
    return DrawResult(
        draws=tuple(
            DrawItem(
                pool_id=pool.pool_id,
                entry_id=entry.entry_id,
                text=entry.text,
                draw_index=draw_index,
                seed=seed,
            )
            for entry in selected
        )
    )


def draw_for_guide(
    config: FlowConfig,
    state: FlowState,
    *,
    source_message_id: object,
    cue_keys: Iterable[str] = (),
    pool_ids: Iterable[str] = (),
) -> DrawResult:
    """Draw stage/cue pools once each, with hard composite caps."""
    if not config.enabled or not state.active:
        return DrawResult()
    stage = config.stage(state.stage)
    if stage is None:
        return DrawResult()
    selected_ids: list[str] = list(stage.pool_ids)
    for key in cue_keys:
        cue = config.cue_for_key(key)
        if cue is not None:
            selected_ids.extend(cue.pool_ids)
    selected_ids.extend(str(item).strip() for item in pool_ids if str(item).strip())
    result: list[DrawItem] = []
    seen_pools: set[str] = set()
    for pool_id in selected_ids:
        if pool_id in seen_pools:
            continue
        seen_pools.add(pool_id)
        pool = config.pool(pool_id)
        if pool is None:
            continue
        drawn = draw_pool(
            pool,
            source_message_id=source_message_id,
            flow_id=config.flow_id,
            cycle=state.cycle,
            flow_started_at=state.started_at,
            draw_index=len(result),
        )
        for item in drawn.draws:
            if len(result) >= MAX_DRAWS:
                return DrawResult(tuple(result))
            current_size = sum(len(entry.text) for entry in result)
            if current_size + len(item.text) > MAX_PRIVATE_TEXT:
                return DrawResult(tuple(result))
            result.append(item)
    return DrawResult(tuple(result))


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
    chunks = [
        '<hidden_flow_guidance flow="%s" stage="%s" cycle="%d" turn="%d">'
        % (
            escape(config.flow_id, quote=True),
            escape(state.stage, quote=True),
            int(state.cycle),
            int(state.stage_turn),
        )
    ]
    used = len(chunks[0])
    for item in result.draws[:MAX_DRAWS]:
        value = escape(item.text, quote=False)
        block = "<anchor pool=\"%s\" id=\"%s\">%s</anchor>" % (
            escape(item.pool_id, quote=True),
            escape(item.entry_id, quote=True),
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
    return "".join(chunks)[:MAX_PRIVATE_TEXT]

