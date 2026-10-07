"""Strict normalization and graph validation for hidden flow definitions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from .types import CueSpec, FlowConfig, PoolEntry, PoolSpec, StageSpec


HIDDEN_FLOW_ENGINE_ENABLED = False
DEFAULT_FLOW_CONFIG = FlowConfig(flow_id="", enabled=False)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _enabled(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _bounded_int(value: Any, *, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _ref_tuple(value: Any, valid: set[str]) -> tuple[str, ...]:
    result: list[str] = []
    for item in _list(value):
        ref = _text(item)
        if ref in valid and ref not in result:
            result.append(ref)
    return tuple(result)


def _normalize_pools(raw_pools: Any) -> tuple[PoolSpec, ...]:
    result: list[PoolSpec] = []
    seen: set[str] = set()
    for raw in _list(raw_pools):
        if not isinstance(raw, Mapping):
            continue
        pool_id = _text(raw.get("id", raw.get("poolId")))
        if not pool_id or pool_id in seen or not _enabled(raw.get("enabled"), False):
            continue
        mode = _text(raw.get("drawMode", raw.get("draw_mode"))).lower()
        if mode not in {"turn", "cycle"}:
            continue
        entries: list[PoolEntry] = []
        for index, raw_entry in enumerate(_list(raw.get("entries"))[:160]):
            if isinstance(raw_entry, Mapping):
                if not _enabled(raw_entry.get("enabled"), True):
                    continue
                text = _text(raw_entry.get("text"))[:1200]
                entry_id = _text(raw_entry.get("id", raw_entry.get("entryId")))
            else:
                text = _text(raw_entry)[:1200]
                entry_id = ""
            if not text:
                continue
            entry_id = entry_id or "entry-%d" % (index + 1)
            if any(item.entry_id == entry_id for item in entries):
                continue
            entries.append(PoolEntry(entry_id=entry_id, text=text))
        result.append(
            PoolSpec(
                pool_id=pool_id,
                enabled=True,
                draw_mode=mode,
                draw_count=_bounded_int(raw.get("drawCount", raw.get("draw_count")), low=1, high=5, default=1),
                entries=tuple(entries),
            )
        )
        seen.add(pool_id)
    return tuple(result)


def _normalize_stages(raw_stages: Any, pool_ids: set[str]) -> tuple[StageSpec, ...]:
    result: list[StageSpec] = []
    seen: set[str] = set()
    for raw in _list(raw_stages):
        if not isinstance(raw, Mapping):
            continue
        stage_id = _text(raw.get("id", raw.get("stageId")))
        if not stage_id or stage_id in seen or not _enabled(raw.get("enabled"), False):
            continue
        next_stage = _text(raw.get("nextStage", raw.get("next_stage"))) or None
        continue_target = _text(raw.get("continueTarget", raw.get("continue_target"))) or None
        result.append(
            StageSpec(
                stage_id=stage_id,
                enabled=True,
                min_turns=_bounded_int(raw.get("minTurns", raw.get("min_turns")), low=1, high=12, default=1),
                repeat_min_turns=_bounded_int(
                    raw.get("repeatMinTurns", raw.get("repeat_min_turns")),
                    low=1,
                    high=12,
                    default=1,
                ),
                next_stage=next_stage,
                continue_target=continue_target,
                terminal_without_continue=_enabled(raw.get("terminalWithoutContinue"), False),
                holdable=_enabled(raw.get("holdable"), False),
                pool_ids=_ref_tuple(raw.get("poolIds", raw.get("pool_ids")), pool_ids),
            )
        )
        seen.add(stage_id)
    return tuple(result)


def _normalize_cues(raw_cues: Any, pool_ids: set[str]) -> tuple[CueSpec, ...]:
    result: list[CueSpec] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for raw in _list(raw_cues):
        if not isinstance(raw, Mapping):
            continue
        cue_id = _text(raw.get("id", raw.get("cueId")))
        key = _text(raw.get("key"))
        folded = key.casefold()
        if not cue_id or not key or cue_id in seen_ids or folded in seen_keys:
            continue
        if not _enabled(raw.get("enabled"), False):
            continue
        result.append(
            CueSpec(
                cue_id=cue_id,
                key=key,
                enabled=True,
                pool_ids=_ref_tuple(raw.get("poolIds", raw.get("pool_ids")), pool_ids),
            )
        )
        seen_ids.add(cue_id)
        seen_keys.add(folded)
    return tuple(result)


def _graph_is_valid(stages: tuple[StageSpec, ...], initial_stage: Optional[str]) -> bool:
    stage_ids = {item.stage_id for item in stages}
    if not initial_stage or initial_stage not in stage_ids:
        return False
    for stage in stages:
        if stage.next_stage is not None and stage.next_stage not in stage_ids:
            return False
        if stage.continue_target is not None and stage.continue_target not in stage_ids:
            return False
        if stage.terminal_without_continue:
            if stage.next_stage is not None or not stage.continue_target:
                return False
        elif not stage.next_stage or stage.continue_target is not None:
            return False
    # The within-cycle path must terminate; a cycle boundary is represented by
    # continueTarget and is therefore checked separately by the engine.
    for stage in stages:
        seen: set[str] = set()
        current: Optional[str] = stage.stage_id
        while current is not None:
            if current in seen:
                return False
            seen.add(current)
            node = next(item for item in stages if item.stage_id == current)
            current = node.next_stage
    return True


def _is_normalized_config(config: FlowConfig) -> bool:
    if type(config.schema_version) is not int or config.schema_version != 1:
        return False
    if not config.flow_id or config.flow_id != config.flow_id.strip():
        return False
    pool_ids = [item.pool_id for item in config.pools]
    if len(pool_ids) != len(set(pool_ids)):
        return False
    for pool in config.pools:
        if not pool.enabled or not pool.pool_id or pool.draw_mode not in {"turn", "cycle"}:
            return False
        if not 1 <= pool.draw_count <= 5 or len(pool.entries) > 160:
            return False
        entry_ids = [item.entry_id for item in pool.entries]
        if len(entry_ids) != len(set(entry_ids)):
            return False
        if any(
            not item.entry_id
            or item.entry_id != item.entry_id.strip()
            or not item.text
            or item.text != item.text.strip()
            or len(item.text) > 1200
            for item in pool.entries
        ):
            return False
    stage_ids = [item.stage_id for item in config.stages]
    if len(stage_ids) != len(set(stage_ids)):
        return False
    for stage in config.stages:
        if not stage.enabled or not stage.stage_id or stage.stage_id != stage.stage_id.strip():
            return False
        if not 1 <= stage.min_turns <= 12 or not 1 <= stage.repeat_min_turns <= 12:
            return False
        if any(pool_id not in pool_ids for pool_id in stage.pool_ids):
            return False
    cue_ids = [item.cue_id for item in config.cues]
    cue_keys = [item.key.casefold() for item in config.cues]
    if len(cue_ids) != len(set(cue_ids)) or len(cue_keys) != len(set(cue_keys)):
        return False
    for cue in config.cues:
        if not cue.enabled or not cue.cue_id or not cue.key or cue.key != cue.key.strip():
            return False
        if any(pool_id not in pool_ids for pool_id in cue.pool_ids):
            return False
    return _graph_is_valid(config.stages, config.initial_stage)


def normalize_flow_config(raw: Any) -> Optional[FlowConfig]:
    """Return a normalized config, or ``None`` when it cannot be trusted."""
    if isinstance(raw, FlowConfig):
        if not raw.flow_id and not raw.enabled and not raw.stages and not raw.cues and not raw.pools:
            return raw if raw.schema_version == 1 else None
        return raw if _is_normalized_config(raw) else None
    if not isinstance(raw, Mapping) or raw.get("schemaVersion") != 1:
        return None
    flow_id = _text(raw.get("flowId", raw.get("id")))
    if not flow_id:
        return None
    pools = _normalize_pools(raw.get("pools"))
    pool_ids = {item.pool_id for item in pools}
    stages = _normalize_stages(raw.get("stages"), pool_ids)
    stage_ids = {item.stage_id for item in stages}
    initial_stage = _text(raw.get("initialStage", raw.get("initial_stage"))) or None
    cues = _normalize_cues(raw.get("cues"), pool_ids)
    if not _graph_is_valid(stages, initial_stage) or initial_stage not in stage_ids:
        return None
    return FlowConfig(
        flow_id=flow_id,
        enabled=_enabled(raw.get("enabled"), False),
        initial_stage=initial_stage,
        stages=stages,
        cues=cues,
        pools=pools,
    )
